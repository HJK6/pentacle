# Managed native launcher

The launcher defaults to `on` when `MIC_SERVER_START_MODE` is absent or empty.
Explicit `off`, `on`, `clipboard`, and `meeting` values are honored; unsupported
values exit before microphone permission or Python startup.

Explicit `off` skips microphone authorization queries and requests. Python loads
the resident model and configured upload vocabulary without opening audio. The
existing microphone permission path remains in place for capture modes. Rebuilding
an ad-hoc signed app changes its designated identity: keeping the bundle identifier
does not preserve microphone authorization. Genuine capture authorization and
hardware validation must be performed separately when the operator is available.

Build an isolated candidate using a verified copy of the installed app as its
metadata template:

```sh
python3 mic-server/macos-app/build.py --template /path/to/original/MicServer.app --output /path/to/owned/candidate/MicServer.app
```

The builder refuses existing output and installed app directories, compiles the
tracked Swift source, ad-hoc signs and verifies the owned candidate, and records
source/binary hashes. It never installs, restarts a service, or modifies the
original template. Retain the full original signed app and launchd/source/config
preimages for reviewed activation and exact rollback.
