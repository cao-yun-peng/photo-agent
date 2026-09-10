"""Server-owned goal and result-batch feedback; independent of model step counts."""

import hashlib
import json
from uuid import uuid4


def new_goal(state):
    state.search_feedback = {
        "version": 1,
        "goal_id": str(uuid4()),
        "batch_id": None,
        "counted_batches": [],
        "rejected_batch_count": 0,
        "scope": "matches",
    }


def record_batch(state, result):
    if not result.get("ok") or not result.get("items"):
        return
    if not state.search_feedback.get("goal_id"):
        new_goal(state)
    feedback = state.search_feedback
    identity = [
        feedback["goal_id"],
        feedback.get("scope", "matches"),
        [str(p["id"]) for p in result["items"] if p.get("id")],
    ]
    # Repeated presentation of the same ordered set is the same batch.
    batch = hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:32]
    feedback["batch_id"] = batch
    feedback["batch_photo_ids"] = identity[-1]
    result["result_batch_id"] = batch
    result["search_goal_id"] = feedback["goal_id"]
    result["browse_scope"] = feedback.get("scope", "matches")


def reject_batch(state):
    feedback = state.search_feedback
    batch = feedback.get("batch_id")
    target = state.feedback_batch_id or batch
    if not batch or target != batch:
        return "stale"
    counted = feedback.setdefault("counted_batches", [])
    if batch in counted:
        return "duplicate"
    # Bounded session bookkeeping; never discard identities and count them again.
    if len(counted) >= 64:
        return "limit"
    counted.append(batch)
    feedback["rejected_batch_count"] = min(
        2, feedback.get("rejected_batch_count", 0) + 1
    )
    state.rejected_photo_ids.update(feedback.get("batch_photo_ids", []))
    return "accepted"


def accept_results(state):
    state.search_feedback["rejected_batch_count"] = 0
    # Preserve consumed batch identities so delayed duplicate feedback stays inert.
