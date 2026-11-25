"""Verify saved authenticated results without reusing credentials or calling services."""
import hashlib
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / "acceptance" / "live"
EXPECTED_COMMIT = "30f6f8ee95ffda5dbc0ff40f47cd5398a1f1f59e"
EXPECTED_IMAGE_SHA = "7f087914eab4b18578a8a285963d7fcc77d89c2ab16bb5d350f22f9ace4cc428"
delivery = json.loads(Path("G:/okr/outputs/bug263855-20261124/final-delivery.json").read_text(encoding="utf-8-sig"))
checks = []
for ref, filename in (
    ("zentao", "zentao-263855-result.json"),
    ("zentao_dragongu", "zentao-dragongu-263855-result.json"),
):
    result = json.loads((LIVE / filename).read_text(encoding="utf-8-sig"))
    context = json.loads(Path(result["context_path"]).read_text(encoding="utf-8-sig"))
    assert result["status"] == "ok"
    assert result["read_only"] is True
    assert context["id"] == "263855"
    assert context["title"] == delivery["title"]
    assert context["status"] == delivery["zentao"]["status"]
    assert context["assigned_to"] == delivery["zentao"]["assignedTo"]
    assert context["steps_text"].strip()
    assert EXPECTED_COMMIT in context["steps_text"]
    assert context["actions"]
    assert "/file-read-437953.png" in context["bug"]["steps"]
    images = [r for r in result["resources"] if r.get("sha256") == EXPECTED_IMAGE_SHA]
    assert len(images) == 1
    image = images[0]
    assert image["status"] == "downloaded"
    assert image["size_bytes"] == 1228296
    assert hashlib.sha256(Path(image["path"]).read_bytes()).hexdigest() == EXPECTED_IMAGE_SHA
    checks.append({
        "credential_ref": ref,
        "status": "passed",
        "fetched_at": result["fetched_at"],
        "bug_id": context["id"],
        "title_status_assignee_match_prior_delivery": True,
        "steps_and_commit_present": True,
        "history_entries": len(context["actions"]),
        "original_image_present": True,
        "image_sha256": EXPECTED_IMAGE_SHA,
        "image_bytes": image["size_bytes"],
        "context_path": result["context_path"],
        "manifest_path": result["manifest_path"],
    })
report = {
    "verified_at": datetime.now().astimezone().isoformat(),
    "scope": "Saved results from independent authenticated CLI fetches; no new network calls by this verifier.",
    "read_only": True,
    "business_writes": False,
    "checks": checks,
    "customer_service_authenticated_acceptance": "pending_credentials",
}
path = LIVE / "zentao-authenticated-acceptance.json"
path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({"status": "passed", "accounts_verified": len(checks), "report": str(path)}))
