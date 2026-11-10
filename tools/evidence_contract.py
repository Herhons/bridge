"""Descriptor validation only; it never manufactures live evidence."""


def web2_complete(events):
    qualified = [row for row in events if row.get("kind") == "raw_record"
                 and row.get("endpoint") == "grok" and row.get("exit") == 0
                 and row.get("round_id")]
    for choose in qualified:
        if choose.get("action") != "choose" or choose.get("model") != "fast":
            continue
        same = [row for row in qualified if row["round_id"] == choose["round_id"]]
        for send in same:
            if send.get("action") != "send_new" or send.get("purpose") != "test" or not send.get("token"):
                continue
            if any(row.get("action") == "read" and row.get("token") == send["token"] for row in same):
                return True
    return False


def workbuddy_restart_complete(events):
    actions = ["close_requested", "closed", "launched", "window", "docs_ready"]
    candidates = [row for row in events if row.get("kind") == "raw_record"
                  and row.get("endpoint") == "workbuddy" and row.get("round_id")
                  and not row.get("close_skipped")]
    for round_id in {row["round_id"] for row in candidates}:
        rows = [row for row in candidates if row["round_id"] == round_id]
        if [row.get("action") for row in rows] == actions and rows[-1].get("docs", 0) > 0:
            return True
    return False
