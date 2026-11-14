"""Mirror of the daemon's derivation, used only to assert the CLI prints its id."""
import hashlib
def expected(composite_stream_id, adoption_key):
    return 'wl-' + hashlib.sha256((composite_stream_id + '\x00' + adoption_key).encode()).hexdigest()[:24]
