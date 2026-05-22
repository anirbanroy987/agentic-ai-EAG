import re
from datetime import datetime, timezone

STOPWORDS = {"the","a","is","my","what","find","to","of","in","and","s"}

def tokenize(text: str) -> set[str]:
    toks = re.findall(r"[a-z0-9]+", text.lower())
    return {t for t in toks if len(t) > 1 and t not in STOPWORDS}

# --- WRITE: the one LLM call extracts keywords once -------------------
# (classifier mocked here; in the file it's the Gemini call in _classify)
def classify(raw_text):
    # pretend the LLM returned this for "My mom's birthday is 15 May 2026"
    return {
        "kind": "fact",
        "keywords": ["mom", "birthday", "may", "2026"],
        "descriptor": "Mom's birthday is 15 May 2026",
        "value": {"entity": "mom", "attribute": "birthday", "value": "2026-05-15"},
    }

store = []
def remember(raw_text):
    cls = classify(raw_text)
    if cls["kind"] == "scratchpad":      # transient -> never persisted
        return None
    cls["created_at"] = datetime.now(timezone.utc)
    store.append(cls)                    # in the file: append + _save() to JSON
    return cls

# --- READ: free, pure-Python keyword overlap --------------------------
def read(query, top_k=8):
    q = tokenize(query)
    scored = []
    for item in store:
        item_tokens = set(item["keywords"]) | tokenize(item["descriptor"])
        score = len(q & item_tokens)     # how many query words it matches
        if score:
            scored.append((score, item))
    scored.sort(key=lambda s: (s[0], s[1]["created_at"]), reverse=True)
    return [item for _, item in scored[:top_k]]


remember("My mom's birthday is 15 May 2026")     # 1 LLM call, happens once
print(read("when is mom's birthday"))             # 0 LLM calls, every time


# At the end of assignment.py
if __name__ == "__main__":
    # Test the functions
    result = remember("My mom's birthday is 15 May 2026")
    print(result)