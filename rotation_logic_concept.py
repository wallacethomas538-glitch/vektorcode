
# Proposed Key Rotation implementation for app.py
# This is a conceptual block to be integrated into the main Handler

KEY_POOLS = {
    "groq": os.environ.get("GROQ_KEYS", "").split(","),
    "gemini": os.environ.get("GEMINI_KEYS", "").split(","),
    "openai": os.environ.get("OPENAI_KEYS", "").split(","),
}
KEY_INDEX = {pid: 0 for pid in KEY_POOLS}

def get_next_key(pid):
    keys = KEY_POOLS.get(pid, [])
    if not keys or not keys[0]: return None
    
    key = keys[KEY_INDEX[pid]]
    KEY_INDEX[pid] = (KEY_INDEX[pid] + 1) % len(keys)
    return key

def call_with_rotation(endpoint, payload, pid):
    for attempt in range(len(KEY_POOLS.get(pid, [None]))):
        key = get_next_key(pid)
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        try:
            req = Request(endpoint, data=payload, headers=headers, method="POST")
            with urlopen(req, timeout=120) as response:
                return json.loads(response.read())
        except HTTPError as e:
            if e.code == 429: # Rate Limit
                continue
            raise e
    raise Exception("All keys in pool exhausted")
