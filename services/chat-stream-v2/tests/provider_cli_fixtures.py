"""Non-network reviewed-pair fixtures for the account suites."""
from _shared.provider_cli_release import digest


def release_pair():
    return {"schema":1,"revision":1,"providers":{
        provider:{"version":version,"artifacts":{
            platform:{"provenance":"https://releases.example.test/"+provider+"/"+version,
                      "native_sha256":"a"*64,"resource_sha256":"b"*64}
            for platform in ("darwin-arm64","linux-x64")}}
        for provider,version in (("claude","2.1.312"),("codex","0.160.0"))}}


async def seed_pin(runtime,value=None):
    from provider_cli_pin import set_pin,raw,current
    value=value or release_pair()
    def write(a):
        record=raw(a)
        if record is not None and current(a)==value: return
        expected=current(a)["revision"] if record is not None else 0
        set_pin(a,{"pin":dict(value,revision=expected+1),"expected_revision":expected,
                   "operation_id":"fixture-pin-"+str(expected+1),"reason":"isolated reviewed fixture"})
    await runtime.run(write)
