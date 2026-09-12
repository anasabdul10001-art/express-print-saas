from slowapi import Limiter
from slowapi.util import get_remote_address

# In-memory, per-process storage - fine for the current single-instance
# deployment. Would need a shared backend (e.g. Redis, via slowapi's
# storage_uri) if this ever runs as multiple worker processes/dynos, since
# each process would otherwise track its own counters.
limiter = Limiter(key_func=get_remote_address)
