"""Planner RED fixtures for spec_pentacle__deploy_prior_receipt_ancestry_2026_11 (cycle-1 repair).
Runs against the pinned 43115384 source via PYTHONPATH. No runtime, no stamp edits.
Every case-2 refusal fixture isolates ONE condition: all other conditions are satisfied."""
import pytest
from deploy import coordinated as c
from deploy.restoration import qualified_prior

BINDING = {'host':'fixture','provider':'claude','alias':'ours','generation':'gen-1',
           'executable_sha256':'1'*64,'version':'2.1.312 (Claude Code)'}
PRIOR = 'a'*40      # the plain-deployed prior being qualified
ANCESTOR = 'c'*40   # the older coordinated target the inherited receipt points at
REFUSAL = 'receipt reference invalid'

def sealed_receipt(path, target_sha):
    receipt = {'schema': c.RECEIPT_SCHEMA, 'intent': {'target_sha': target_sha, 'prior_sha': 'b'*40},
               'receipt_path': str(path), 'accounts': {}, 'scope': {}}
    c.atomic_json(path, c.seal_receipt(receipt))
    return c.load_receipt(path)

def forward_qualified(sha):
    return {'sha': sha, 'daemon_runtime_readback': {'sha': sha},
            'daemon_boot_readback': {'outcome': 'observed'},
            'fleet_smoke': {'outcome': 'passed', 'cells': [
                {'host':'fixture','provider':'claude','prompt_mode':m,'outcome':'passed','account_binding':dict(BINDING)}
                for m in ('prompted','promptless')]}}

def with_ancestry(stamp, pointer, nested_pointer=None):
    """Plain-forward writer shape: inherited pointer plus the nested prior witness carrying the same pointer."""
    stamp['coordinated_receipt'] = pointer
    stamp['prior_qualification'] = {'sha': PRIOR, 'stamp': {'sha': PRIOR, 'coordinated_receipt': nested_pointer or pointer}}
    return stamp

# --- case 2: plain-deploy ancestry (the live 1e449706 shape) -------------------------------------

def test_live_shape_plain_prior_with_inherited_ancestry_pointer(tmp_path):
    """Inherited pointer, no receipt_sha256, genuine receipt targeting an ancestor, nested witness matches."""
    receipt = sealed_receipt(tmp_path/'receipt.json', ANCESTOR)
    stamp = with_ancestry(forward_qualified(PRIOR), str(tmp_path/'receipt.json'))
    ref = qualified_prior(stamp, PRIOR, [BINDING])
    assert ref['sha'] == PRIOR and ref['stamp_sha256'] == c.digest(stamp)
    assert ref['ancestry_receipt'] == stamp['coordinated_receipt']
    assert ref['ancestry_receipt_sha256'] == receipt['receipt_sha256']
    assert 'receipt' not in ref and 'receipt_sha256' not in ref

def test_malformed_ancestry_seal_refuses(tmp_path):
    """Only the seal is wrong: no seal claim, inheritance matches, foreign target."""
    receipt = sealed_receipt(tmp_path/'receipt.json', ANCESTOR)
    broken = dict(receipt); broken['receipt_sha256'] = 'f'*64
    c.atomic_json(tmp_path/'receipt.json', broken)
    stamp = with_ancestry(forward_qualified(PRIOR), str(tmp_path/'receipt.json'))
    with pytest.raises(c.Invalid, match=REFUSAL):
        qualified_prior(stamp, PRIOR, [BINDING])

@pytest.mark.parametrize('claim', ['real_seal', '', None])
def test_ancestry_with_seal_claim_refuses(tmp_path, claim):
    """Only the seal claim is wrong: genuine foreign-target receipt, inheritance matches, stamp claims a seal
    (a real seal, an empty string or None). Covers coordinated-rollback stamps and a `not stamp.get(...)` shortcut."""
    receipt = sealed_receipt(tmp_path/'receipt.json', ANCESTOR)
    stamp = with_ancestry(forward_qualified(PRIOR), str(tmp_path/'receipt.json'))
    stamp['receipt_sha256'] = receipt['receipt_sha256'] if claim == 'real_seal' else claim
    with pytest.raises(c.Invalid, match=REFUSAL):
        qualified_prior(stamp, PRIOR, [BINDING])

@pytest.mark.parametrize('shape', ['absent', 'no_nested_pointer', 'different_pointer', 'not_a_dict', 'stamp_not_a_dict'])
def test_ancestry_without_inheritance_proof_refuses(tmp_path, shape):
    """Only the inheritance proof is wrong: genuine foreign-target receipt, no seal claim."""
    sealed_receipt(tmp_path/'receipt.json', ANCESTOR)
    stamp = with_ancestry(forward_qualified(PRIOR), str(tmp_path/'receipt.json'))
    if shape == 'absent': del stamp['prior_qualification']
    if shape == 'no_nested_pointer': del stamp['prior_qualification']['stamp']['coordinated_receipt']
    if shape == 'different_pointer': stamp['prior_qualification']['stamp']['coordinated_receipt'] = str(tmp_path/'other.json')
    if shape == 'not_a_dict': stamp['prior_qualification'] = 'legacy'
    if shape == 'stamp_not_a_dict': stamp['prior_qualification']['stamp'] = None
    with pytest.raises(c.Invalid, match=REFUSAL):
        qualified_prior(stamp, PRIOR, [BINDING])

def test_missing_ancestry_receipt_file_refuses(tmp_path):
    stamp = with_ancestry(forward_qualified(PRIOR), str(tmp_path/'absent.json'))
    with pytest.raises(c.Invalid):
        qualified_prior(stamp, PRIOR, [BINDING])

# --- case 1: the prior's own sealed receipt (unchanged behaviour) ---------------------------------

def test_sealed_prior_with_stripped_seal_still_refuses(tmp_path):
    """Receipt targets PRIOR itself: it is the prior's own receipt. Even with a matching nested witness,
    a stamp without receipt_sha256 must not qualify through the ancestry path."""
    sealed_receipt(tmp_path/'receipt.json', PRIOR)
    stamp = with_ancestry(forward_qualified(PRIOR), str(tmp_path/'receipt.json'))
    with pytest.raises(c.Invalid, match=REFUSAL):
        qualified_prior(stamp, PRIOR, [BINDING])

def test_sealed_prior_intact_binds_receipt(tmp_path):
    """Coordinated-forward writer shape: pointer + seal, no prior_qualification."""
    receipt = sealed_receipt(tmp_path/'receipt.json', PRIOR)
    stamp = forward_qualified(PRIOR)
    stamp.update(coordinated_receipt=str(tmp_path/'receipt.json'), receipt_sha256=receipt['receipt_sha256'])
    ref = qualified_prior(stamp, PRIOR, [BINDING])
    assert ref['receipt'] == str(tmp_path/'receipt.json') and ref['receipt_sha256'] == receipt['receipt_sha256']
    assert 'ancestry_receipt' not in ref

def test_sealed_prior_with_wrong_seal_claim_refuses(tmp_path):
    receipt = sealed_receipt(tmp_path/'receipt.json', PRIOR)
    stamp = forward_qualified(PRIOR)
    stamp.update(coordinated_receipt=str(tmp_path/'receipt.json'), receipt_sha256='e'*64)
    with pytest.raises(c.Invalid, match=REFUSAL):
        qualified_prior(stamp, PRIOR, [BINDING])
