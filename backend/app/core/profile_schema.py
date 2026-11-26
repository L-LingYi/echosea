"""Four-Dimensional Member Profile Schema, Privacy Scrubbing, Anti-Joke Confirmation & 100-Char Prompt Brief."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

# Familiarity gradient thresholds
TIER_LABELS = {
    "stranger": "初识",
    "acquaintance": "熟脸",
    "close_pal": "老熟人",
}

VALID_TRAIT_CATEGORIES = ("interests", "expertise", "schedule_habits")

# Sensitive PII & private-leak patterns blocked from profile extraction (especially public_profile)
_PII_PATTERNS = (
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),  # CN mobile phone
    re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),  # CN ID card
    re.compile(r"(?<!\d)\d{16,19}(?!\d)"),  # bank card
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),  # email
    re.compile(r"(?:sk-|ghp_|xox[baprs]-)[A-Za-z0-9_-]{8,}", re.IGNORECASE),  # API keys/tokens
    re.compile(r"(?:密码|验证码|口令|身份证|银行卡|住址|门牌号|月薪|年薪|工资)\s*[:：]?\s*[A-Za-z0-9\u4e00-\u9fff]{2,}"),
)

_SENSITIVE_PUBLIC_KEYWORDS = (
    "身份证",
    "手机号",
    "银行卡",
    "住址",
    "家庭住址",
    "密码",
    "验证码",
    "工资条",
    "月薪",
    "年薪",
    "确诊",
    "病历",
    "抑郁症",
    "隐私",
)


def relationship_tier(relationship_level: int | None) -> tuple[str, str]:
    """Map numeric relationship_level to familiarity tier (key, Chinese label)."""
    lvl = int(relationship_level or 0)
    if lvl >= 35:
        return "close_pal", TIER_LABELS["close_pal"]
    if lvl >= 10:
        return "acquaintance", TIER_LABELS["acquaintance"]
    return "stranger", TIER_LABELS["stranger"]


def is_sensitive_or_private_text(text: str, *, strict_public: bool = True) -> bool:
    """Return True if text contains PII or sensitive private facts that must not be stored."""
    clean = (text or "").strip()
    if not clean:
        return True
    for pat in _PII_PATTERNS:
        if pat.search(clean):
            return True
    if strict_public and any(kw in clean for kw in _SENSITIVE_PUBLIC_KEYWORDS):
        return True
    return False


def sanitize_clue_text(text: str, max_len: int = 32, *, strict_public: bool = True) -> str:
    """Normalize a profile clue and return empty string if sensitive or invalid."""
    clean = re.sub(r"\s+", " ", (text or "").strip())
    if not clean or len(clean) > max_len:
        return ""
    if is_sensitive_or_private_text(clean, strict_public=strict_public):
        return ""
    return clean


def _parse_iso_dt(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def prune_expired_states(states: list[Any], now_utc: datetime | None = None) -> list[dict[str, str]]:
    """Keep only valid, non-expired recent_states with mandatory expires_at."""
    now = now_utc or datetime.now(timezone.utc)
    kept: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in states or []:
        if not isinstance(item, dict):
            continue
        text = sanitize_clue_text(str(item.get("text") or ""), max_len=40, strict_public=False)
        if not text or text in seen:
            continue
        exp = _parse_iso_dt(item.get("expires_at"))
        if exp is None or exp <= now:
            continue
        seen.add(text)
        kept.append(
            {
                "text": text,
                "created_at": str(item.get("created_at") or now.isoformat()),
                "expires_at": exp.isoformat(),
            }
        )
    return kept[-6:]


def normalize_profile(
    raw_profile: dict[str, Any] | None,
    relationship_level: int = 0,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Normalize a public_profile or private_profile JSONB dict into the 4D schema (backward-compatible)."""
    now = now_utc or datetime.now(timezone.utc)
    src = dict(raw_profile or {})

    rel_in = src.get("relationship") if isinstance(src.get("relationship"), dict) else {}
    tier_key, _ = relationship_tier(relationship_level)
    relationship = {
        "familiarity_tier": str(rel_in.get("familiarity_tier") or tier_key),
        "preferred_address": str(rel_in.get("preferred_address") or "").strip()[:24],
        "interaction_style": str(rel_in.get("interaction_style") or "").strip()[:24],
        "boundaries": list(
            dict.fromkeys(
                str(b).strip()[:32]
                for b in (rel_in.get("boundaries") or [])
                if str(b).strip()
            )
        )[:8],
    }
    # Always keep familiarity_tier aligned with live relationship_level
    relationship["familiarity_tier"] = tier_key

    traits_in = src.get("stable_traits") if isinstance(src.get("stable_traits"), dict) else {}
    legacy_interests = src.get("interests") if isinstance(src.get("interests"), list) else []
    raw_interests = [*(traits_in.get("interests") or []), *legacy_interests]
    stable_traits = {
        "interests": list(dict.fromkeys(str(x).strip()[:24] for x in raw_interests if str(x).strip()))[:16],
        "expertise": list(
            dict.fromkeys(str(x).strip()[:24] for x in (traits_in.get("expertise") or []) if str(x).strip())
        )[:12],
        "schedule_habits": list(
            dict.fromkeys(str(x).strip()[:24] for x in (traits_in.get("schedule_habits") or []) if str(x).strip())
        )[:8],
    }

    candidates: list[dict[str, Any]] = []
    seen_cand: set[tuple[str, str]] = set()
    for c in src.get("candidate_clues") or []:
        if not isinstance(c, dict):
            continue
        cat = str(c.get("category") or "interests")
        if cat not in VALID_TRAIT_CATEGORIES:
            continue
        val = str(c.get("value") or "").strip()[:24]
        if not val or val in stable_traits[cat] or (cat, val) in seen_cand:
            continue
        seen_cand.add((cat, val))
        last_dt = _parse_iso_dt(c.get("last_seen_at"))
        if last_dt is not None and (now - last_dt) > timedelta(days=30):
            continue
        candidates.append(
            {
                "category": cat,
                "value": val,
                "count": max(1, int(c.get("count") or 1)),
                "first_seen_date": str(c.get("first_seen_date") or now.date().isoformat()),
                "last_seen_date": str(c.get("last_seen_date") or now.date().isoformat()),
                "last_seen_at": (last_dt or now).isoformat(),
            }
        )

    recent_states = prune_expired_states(src.get("recent_states") or [], now_utc=now)

    return {
        "relationship": relationship,
        "stable_traits": stable_traits,
        "candidate_clues": candidates[-24:],
        "recent_states": recent_states,
        "interests": list(stable_traits["interests"]),
    }


def observe_profile_clue(
    profile: dict[str, Any],
    category: str,
    value: str,
    msg_date_str: str,
    now_utc: datetime | None = None,
    *,
    strict_public: bool = True,
) -> bool:
    """Record a trait clue with anti-joke confirmation:
    - 1st mention enters candidate_clues (not yet promoted to stable_traits).
    - 2nd mention (count >= 2) OR cross-day mention (msg_date != first_seen_date) promotes to stable_traits.
    Returns True if profile was modified.
    """
    if category not in VALID_TRAIT_CATEGORIES:
        return False
    clean_val = sanitize_clue_text(value, max_len=24, strict_public=strict_public)
    if not clean_val:
        return False

    now = now_utc or datetime.now(timezone.utc)
    traits = profile.setdefault("stable_traits", {"interests": [], "expertise": [], "schedule_habits": []})
    cat_list = list(traits.get(category) or [])
    if clean_val in cat_list:
        return False

    candidates = list(profile.get("candidate_clues") or [])
    matched_idx: int | None = None
    for idx, c in enumerate(candidates):
        if isinstance(c, dict) and c.get("category") == category and c.get("value") == clean_val:
            matched_idx = idx
            break

    if matched_idx is None:
        candidates.append(
            {
                "category": category,
                "value": clean_val,
                "count": 1,
                "first_seen_date": msg_date_str,
                "last_seen_date": msg_date_str,
                "last_seen_at": now.isoformat(),
            }
        )
        profile["candidate_clues"] = candidates[-24:]
        return True

    cand = dict(candidates[matched_idx])
    new_count = int(cand.get("count") or 1) + 1
    cross_day = bool(cand.get("first_seen_date") and cand["first_seen_date"] != msg_date_str)
    if new_count >= 2 or cross_day:
        cat_list.append(clean_val)
        traits[category] = list(dict.fromkeys(cat_list))[:16]
        if category == "interests":
            profile["interests"] = list(traits["interests"])
        candidates.pop(matched_idx)
        profile["candidate_clues"] = candidates
        return True

    cand["count"] = new_count
    cand["last_seen_date"] = msg_date_str
    cand["last_seen_at"] = now.isoformat()
    candidates[matched_idx] = cand
    profile["candidate_clues"] = candidates
    return True


def observe_recent_state(
    profile: dict[str, Any],
    text: str,
    now_utc: datetime | None = None,
    ttl_days: int = 10,
    *,
    strict_public: bool = True,
) -> bool:
    """Append or refresh a temporary recent state with mandatory 7-14 day TTL."""
    clean = sanitize_clue_text(text, max_len=36, strict_public=strict_public)
    if not clean:
        return False
    now = now_utc or datetime.now(timezone.utc)
    clamped_days = max(7, min(14, int(ttl_days)))
    exp = now + timedelta(days=clamped_days)

    states = prune_expired_states(profile.get("recent_states") or [], now_utc=now)
    updated = [s for s in states if s["text"] != clean]
    updated.append({"text": clean, "created_at": now.isoformat(), "expires_at": exp.isoformat()})
    profile["recent_states"] = updated[-6:]
    return True


def format_acquaintance_brief(
    acquaintance_card: dict[str, Any] | None,
    scene: str,
    max_chars: int = 100,
    now_utc: datetime | None = None,
) -> str:
    """Render a compact <= 100-char 4D speaker summary for Prompt injection.
    Strictly enforces public-only isolation when scene == 'group'.
    """
    if not acquaintance_card:
        return "初识群友"

    now = now_utc or datetime.now(timezone.utc)
    lvl = int(acquaintance_card.get("relationship_level") or 0)
    _, tier_label = relationship_tier(lvl)

    pub = normalize_profile(acquaintance_card.get("public_profile") or {}, relationship_level=lvl, now_utc=now)
    profiles = [pub]
    if scene == "private" and isinstance(acquaintance_card.get("private_profile"), dict):
        priv = normalize_profile(acquaintance_card["private_profile"], relationship_level=lvl, now_utc=now)
        profiles.append(priv)

    parts: list[str] = [tier_label]

    styles = [p["relationship"]["interaction_style"] for p in profiles if p["relationship"]["interaction_style"]]
    if styles:
        parts.append(styles[-1])

    addrs = [p["relationship"]["preferred_address"] for p in profiles if p["relationship"]["preferred_address"]]
    if addrs:
        parts.append(f"习惯叫:{addrs[-1]}")

    boundaries = list(dict.fromkeys(b for p in profiles for b in p["relationship"]["boundaries"]))
    if boundaries:
        parts.append(f"雷区:{'/'.join(boundaries[:2])}")

    traits = list(
        dict.fromkeys(
            x
            for p in profiles
            for cat in ("interests", "expertise", "schedule_habits")
            for x in p["stable_traits"][cat]
        )
    )
    if traits:
        parts.append(f"特点:{'/'.join(traits[:4])}")

    states = list(dict.fromkeys(s["text"] for p in profiles for s in p["recent_states"]))
    if states:
        parts.append(f"近况:{states[-1]}")

    summary = "；".join(parts)
    if len(summary) > max_chars:
        summary = summary[: max(1, max_chars - 1)] + "…"
    return summary
