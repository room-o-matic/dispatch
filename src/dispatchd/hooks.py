"""Webhook ingress: the caller supplies the prompt; the webhook's config fixes everything
else (the template, the task wording around the prompt, the limits).

Every request is signed with the webhook's secret (from the env var its config names):

    X-Rom-Timestamp: <unix seconds>
    X-Rom-Signature: sha256=<hex HMAC-SHA256(secret, f"{timestamp}.{raw body}")>
    X-Rom-Delivery:  <caller's unique id for this delivery>   (POST only)

A timestamp more than five minutes off is refused, so a captured request can't be
replayed later; a repeated delivery id returns the original run instead of starting
another. The body is {"prompt": str, "goal"?: str} and nothing else: unknown keys are
rejected, so a caller can't even try to override the restrictions. The prompt is
untrusted text and only ever lands inside the operator's `task_template`.
"""

import hashlib
import hmac
import json
import re
import time

MAX_SKEW_SECONDS = 300
DELIVERY = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")
ALLOWED_KEYS = {"prompt", "goal"}
GOAL_LIMIT = 2000


class HookError(Exception):
    def __init__(self, status: int, detail: str, headers: dict | None = None):
        super().__init__(detail)
        self.status, self.detail, self.headers = status, detail, headers or {}


def sign(secret: str, timestamp: int | str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def verify(secret: str, headers, body: bytes, now: float | None = None) -> None:
    ts, sig = headers.get("x-rom-timestamp"), headers.get("x-rom-signature")
    if not ts or not sig:
        raise HookError(401, "missing X-Rom-Timestamp or X-Rom-Signature")
    if not ts.isdigit() or abs((now or time.time()) - int(ts)) > MAX_SKEW_SECONDS:
        raise HookError(401, "timestamp missing, malformed or more than 5 minutes off")
    if not hmac.compare_digest(sign(secret, ts, body), sig):
        raise HookError(401, "bad signature")


def parse(body: bytes, max_prompt_bytes: int) -> tuple[str, str | None]:
    try:
        data = json.loads(body)
    except ValueError as e:
        raise HookError(400, "body is not JSON") from e
    if not isinstance(data, dict):
        raise HookError(422, "body must be a JSON object")
    if extra := set(data) - ALLOWED_KEYS:
        raise HookError(
            422,
            f"unknown keys {sorted(extra)}: only prompt and goal are accepted; everything "
            "else is fixed by the webhook's configuration",
        )
    prompt, goal = data.get("prompt"), data.get("goal")
    if not isinstance(prompt, str) or not prompt.strip():
        raise HookError(422, "prompt is required (a non-empty string)")
    if len(prompt.encode()) > max_prompt_bytes:
        raise HookError(413, f"prompt is over {max_prompt_bytes} bytes")
    if goal is not None and (not isinstance(goal, str) or len(goal) > GOAL_LIMIT):
        raise HookError(422, f"goal must be a string of at most {GOAL_LIMIT} characters")
    return prompt, (goal.strip() or None) if goal else None


def render(task_template: str, prompt: str) -> str:
    # Plain substitution, not str.format: braces in the prompt or template are just text.
    return task_template.replace("{prompt}", prompt.strip())
