"""Private host mutation ordering and immutable abandonment seals.

All entry points run under Registry.lock, the same nonblocking process lock as
native work and registry publication. The authenticated coordinator owns permits;
ordinary helper mutation cannot manufacture one by omitting coordinator dispatch.
"""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import time

from provider_accounts import AccountError, Binding, _component, _mkdir, _private, write_private_json


def _path(host,group,operation):
    return host.journal/group/(_component(operation)+".json")


def read(host,group,operation):
    path=_path(host,group,operation)
    if not path.exists():return None
    _private(path,directory=False)
    try:return json.loads(path.read_text())
    except (ValueError,TypeError):raise AccountError("account_operation_corrupt") from None


def write(host,group,operation,value):
    path=_path(host,group,operation);_mkdir(path.parent)
    write_private_json(path,value)


def selected(host,provider,alias):
    value=host.registry.active(provider,alias,host.config["host"])
    return value.public() if value else None


def _cell_entries(host,group,provider,alias):
    root=host.journal/group
    result={}
    if root.exists():
        for path in sorted(root.glob("*.json")):
            value=read(host,group,path.stem)
            if value.get("provider")==provider and value.get("alias")==alias:result[path.stem]=value
    return result


def status(host,provider,alias):
    from provider_cli_artifact import code_identity
    return {"outcome":"ok","binding":selected(host,provider,alias),
        "permits":_cell_entries(host,"permits",provider,alias),
        "seals":_cell_entries(host,"seals",provider,alias),
        "code_identity":code_identity(),"mutation_clearance_required":host.config.get("mutation_clearance")=="required"}


def authorize(host,action,identity,operation,clearance):
    if read(host,"seals",operation):raise AccountError("account_operation_sealed")
    if host.config.get("mutation_clearance")!="required":return
    permit=read(host,"permits",operation)
    if (not isinstance(clearance,str) or not permit or permit.get("clearance_id")!=clearance
            or permit.get("action")!=action or any(permit.get(k)!=v for k,v in identity.items())):
        raise AccountError("account_mutation_clearance_required")


def reserve(host,provider,alias,operation,context):
    from provider_cli_pin import digest
    from provider_cli_artifact import assert_drained
    required={"action","expected_generation","generation","clearance_id","host_binding","central_intent"}
    if not isinstance(context,dict) or not required<=context.keys():raise AccountError("account_mutation_clearance_required")
    if context["action"] not in {"repin","repin-reconcile","enroll","adopt"}:raise AccountError("account_action_invalid")
    prior=read(host,"permits",operation)
    value={**context,"provider":provider,"alias":alias}
    if prior:
        if prior!=value:raise AccountError("account_operation_conflict")
        if read(host,"seals",operation):raise AccountError("account_operation_sealed")
        return {"outcome":"ok","permit":prior}
    if selected(host,provider,alias)!=context["host_binding"]:raise AccountError("account_login_generation_stale")
    if read(host,"seals",operation):raise AccountError("account_operation_sealed")
    for op,p in _cell_entries(host,"permits",provider,alias).items():
        if read(host,"seals",op):continue
        journal=host._read_operation(op)
        if not (journal and journal.get("state")=="done"):
            raise AccountError("account_mutation_unresolved")
    if not isinstance(context["clearance_id"],str) or len(context["clearance_id"])!=64:
        raise AccountError("account_mutation_clearance_required")
    write(host,"permits",operation,value)
    return {"outcome":"ok","permit":value}


def inspect(host,provider,alias,operation,context,*,seal=False):
    from provider_cli_pin import digest
    from provider_cli_artifact import assert_drained
    if not isinstance(context,dict) or not isinstance(context.get("arguments"),dict):
        raise AccountError("account_repin_receipt_invalid")
    args=context["arguments"];pin=context["pin"];base=Binding(**context["base"])
    from account_repin import target_matches
    if not target_matches(args,pin):raise AccountError("account_repin_receipt_invalid")
    identity={"provider":provider,"alias":alias,"expected_generation":args["login_generation"],
        "generation":args["new_generation"],"pin_revision":pin["revision"],"pin_digest":digest(pin)}
    if (base.host!=host.config["host"] or (base.provider,base.alias)!=(provider,alias)
            or args.get("host")!=base.host or args.get("provider")!=provider or args.get("account")!=alias):
        raise AccountError("account_repin_receipt_invalid")
    receipt=host._read_operation(operation);target=host._read_operation(operation+"-target")
    for value in (receipt,target):
        if value and (value.get("provider"),value.get("alias"))!=(provider,alias):
            raise AccountError("account_operation_conflict")
    actual=host.registry.active(provider,alias,host.config["host"])
    result={"outcome":"ok","identity":identity,"receipt":receipt,"target":target,
        "binding":actual.public() if actual else None,"seal":read(host,"seals",operation)}
    if receipt and any(receipt.get(k)!=v for k,v in identity.items()):
        raise AccountError("account_operation_conflict")
    if result["seal"]:
        result["effect"]="zero" if (result["seal"].get("worker_quiescent") is True and actual and actual.public()==base.public()) else "conflict"
        return result
    raw_cell=host.config.get("cells",{}).get(provider+":"+alias,{})
    if (receipt and receipt.get("kind")=="repin" and receipt.get("state")!="done"
            and receipt.get("proof") and actual and actual.generation==args["new_generation"]):
        receipt=host._repin("repin-status",actual,raw_cell,identity,operation,pin)
        result["receipt"]=receipt
    if target and target.get("state")=="done" and target.get("outcome")=="ok" and (not receipt or receipt.get("state")!="done"):
        # Complete proof was persisted by the target before registry activation.
        # Finalize only the missing wrapper; never rerun the target or relabel
        # its historical artifact proof as a new current verification.
        target_args={"host":base.host,"provider":provider,"account":alias,"login_generation":base.generation,
            "new_generation":args["new_generation"],"expected_pin_revision":pin["revision"],"expected_pin_digest":digest(pin)}
        link={"operation_id":operation+"-target","old":asdict(base),"arguments":target_args,
            "pin":pin,"receipt":target,"consumer_code_identity":context["consumer_code_identity"]}
        links=context["links"]+[link]
        host._validate_repin_links({"root":context["root"],"links":links},raw_cell,require_active=False)
        receipt={**identity,"kind":"repin-reconcile","reconcile":{"root":context["root"],"links":context["links"]},
            "state":"done","outcome":"ok","links":links,"binding":target["binding"],
            "historical_target_artifact":target["proof"],"recovered_from_target":operation+"-target"}
        host._save_operation(operation,receipt);result["receipt"]=receipt
    # A complete target may precede wrapper completion; never erase that effect.
    if any(v and v.get("state")=="done" and v.get("outcome")=="ok" for v in (receipt,target)):
        result["effect"]="success";return result
    if (not actual or actual.public()!=base.public()):
        result["effect"]="conflict";return result
    host.registry._validate_home(actual)
    proposed=":".join((base.host,provider,alias,args["new_generation"]))
    registry=host.registry._load();collision=registry["generations"].get(proposed)
    collision_proof=None
    if collision:
        permit=read(host,"permits",operation)
        for path in host.journal.glob("*.json"):
            value=host._read_operation(path.stem)
            if (path.stem not in {operation,operation+"-target"} and value and value.get("state")=="done"
                    and value.get("outcome")=="ok" and (value.get("binding") or {}).get("generation")==args["new_generation"]
                    and value.get("finished_at_ns",2**100)<(permit or {}).get("reserved_at_ns",0)):
                candidate=Binding(**collision)
                if candidate.public()==value["binding"]:collision_proof={"operation_id":path.stem,"receipt":value}
        if collision_proof is None:result["effect"]="conflict";return result
    # Successful Registry.lock acquisition excludes every journal/registry
    # publisher. A separate provider pane cannot publish a generation; its
    # occupancy still independently refuses a subsequent native repin.
    result["effect"]="zero"
    if seal:
        proof={"state":"sealed","outcome":"operation_sealed","provider":provider,"alias":alias,
            "operation_id":operation,"identity":identity,"arguments_digest":digest(args),
            "base":base.public(),"successor_absent":not bool(collision),"collision":collision_proof,
            "worker_quiescent":True,"journal_digest":digest({"receipt":receipt,"target":target})}
        for op in (operation,operation+"-target"):
            previous=read(host,"seals",op)
            if previous and previous!=proof:raise AccountError("account_operation_conflict")
            if not previous:write(host,"seals",op,proof)
        result["seal"]=proof
    return result


def supersede(host,provider,alias,operation,context):
    from provider_cli_artifact import assert_drained
    from provider_cli_pin import digest
    if not isinstance(context,dict) or selected(host,provider,alias)!=context.get("host_binding"):
        raise AccountError("account_repin_state_changed")
    active=host.registry.active(provider,alias,host.config["host"])
    if active:assert_drained(active,run=host.run,host=host,launchers=context.get("managed_launchers",[]))
    prior=read(host,"supersessions",operation)
    if prior:
        if prior["snapshot_digest"]!=context.get("snapshot_digest"):raise AccountError("account_operation_conflict")
        return prior
    # FD's digest includes every durable host permit and seal. Actual selection
    # alone cannot detect a delayed executor reservation or abandonment record.
    # Compare under the same flock before the first supersession side effect.
    if (context.get("expected_permits")!=_cell_entries(host,"permits",provider,alias)
            or context.get("expected_seals")!=_cell_entries(host,"seals",provider,alias)):
        raise AccountError("account_repin_state_changed")
    operations=context.get("operations")
    if not isinstance(operations,list):raise AccountError("account_repin_supersession_empty")
    lineage=context.get("retained_lineage")
    if not operations and not lineage:raise AccountError("account_repin_supersession_empty")
    if lineage:
        final=host._validate_repin_links(lineage,host.config["cells"][provider+":"+alias])
        if final.public()!=context["host_binding"]:raise AccountError("account_repin_state_changed")
    result={"outcome":"ok","provider":provider,"alias":alias,"snapshot_digest":context["snapshot_digest"],
        "host_binding":context["host_binding"],"operations":operations,"worker_quiescent":True}
    for op in operations:
        _component(op)
        for ident in (op,op+"-target"):
            if not read(host,"seals",ident):
                write(host,"seals",ident,{"state":"sealed","outcome":"superseded","provider":provider,"alias":alias,
                    "operation_id":op,"superseded_by":operation,"snapshot_digest":context["snapshot_digest"]})
    write(host,"supersessions",operation,result)
    return result
