"""
core/analysis.py
================
AI-powered analysis of daily body tracking sessions.

Uses the Anthropic API (Claude) to:
  1. Interpret today's measurements in context of recent history
  2. Correlate scale metrics with wearable context (sleep, HRV, activity)
  3. Detect meaningful trends (vs noise)
  4. Generate actionable, personalised recommendations

Design principles:
  - The prompt is fully data-driven — no hard-coded health advice
  - All AI output is clearly labelled as AI-generated
  - The module is stateless: all context comes from the DB query
  - Runs asynchronously and never blocks the measurement session
  - Gracefully degrades if the API is unavailable

Configuration:
    ai:
      enabled: true
      provider: "anthropic"
      api_key: "sk-ant-..."
      model: "claude-sonnet-4-20250514"
      context_days: 30          # how many past sessions to include
      analyse_photo: false       # future: vision-based silhouette analysis
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from adapters.base import ScaleReading, ContextReading
from core.database import Database

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public entry point called from scheduler
# ---------------------------------------------------------------------------

async def analyse_session(
    db: Database,
    session_id: int,
    reading: ScaleReading,
    context: list[ContextReading],
    config: dict,
) -> Optional[dict]:
    """
    Build a prompt from today's data + recent history, call Claude,
    save the result to ai_insights, and return it.

    Returns a dict with keys: summary, trends, recommendations, model_used
    Returns None if analysis is disabled or the API call fails.
    """
    if not config.get("enabled", False):
        return None

    api_key = config.get("api_key", "")
    if not api_key or api_key.startswith("YOUR_"):
        logger.warning("AI analysis skipped: no valid API key configured")
        return None

    model = config.get("model", "claude-sonnet-4-20250514")
    context_days = config.get("context_days", 30)

    # Fetch recent history for trend context
    profile_id = await _get_profile_id(db, session_id)
    history = await _build_history(db, profile_id, context_days)
    wearable_history = await _build_wearable_history(db, profile_id, context_days)

    # Build the prompt
    prompt = _build_prompt(reading, context, history, wearable_history)

    # Call Claude
    try:
        result = await _call_claude(prompt, model, api_key)
    except Exception as exc:
        logger.error("AI analysis API call failed: %s", exc)
        return None

    # Parse structured response
    parsed = _parse_response(result)

    # Save to DB
    await db.save_ai_insight(
        session_id=session_id,
        model_used=model,
        summary=parsed.get("summary", ""),
        trends=parsed.get("trends", ""),
        recommendations=parsed.get("recommendations", ""),
        raw_response=result,
    )

    logger.info("AI analysis saved for session %d", session_id)
    return {**parsed, "model_used": model}


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def _build_prompt(
    reading: ScaleReading,
    context: list[ContextReading],
    history: list[dict],
    wearable_history: dict[str, list],
) -> str:
    """
    Build a structured prompt that gives Claude all the data it needs
    to produce a meaningful, personalised analysis.
    """
    lines = [
        "You are a body composition and health analyst for a personal tracking system.",
        "Analyse today's measurement and provide actionable insights.",
        "Be concise, specific, and avoid generic health advice.",
        "Base all observations on the actual data provided.",
        "",
        "## Today's measurement",
        f"Date: {reading.recorded_at.strftime('%Y-%m-%d %H:%M UTC')}",
        f"Weight: {reading.weight_kg:.1f} kg",
    ]

    # Scale metrics
    metric_map = {
        "body_fat_pct": ("Body fat", "%"),
        "muscle_mass_kg": ("Muscle mass", "kg"),
        "bone_mass_kg": ("Bone mass", "kg"),
        "water_pct": ("Total body water", "%"),
        "visceral_fat_index": ("Visceral fat index", ""),
        "bmr_kcal": ("Basal metabolic rate", "kcal"),
        "phase_angle": ("Phase angle", "°"),
        "ecm_bcm_ratio": ("ECM/BCM ratio", ""),
        "heart_rate_bpm": ("Heart rate", "bpm"),
        "pulse_wave_velocity": ("Pulse wave velocity", "m/s"),
        "vascular_age": ("Vascular age", "years"),
        "nerve_health_score": ("Nerve health score", ""),
    }
    for attr, (label, unit) in metric_map.items():
        val = getattr(reading, attr, None)
        if val is not None:
            suffix = f" {unit}" if unit else ""
            lines.append(f"{label}: {val}{suffix}")

    for key, val in reading.extras.items():
        lines.append(f"{key.replace('_', ' ').title()}: {val}")

    # Wearable context for today
    if context:
        lines.append("\n## Today's wearable context")
        for ctx in context:
            lines.append(f"\nSource: {ctx.source}")
            ctx_map = {
                "sleep_score": "Sleep score",
                "sleep_duration_h": "Sleep duration (h)",
                "deep_sleep_h": "Deep sleep (h)",
                "rem_sleep_h": "REM sleep (h)",
                "sleep_efficiency_pct": "Sleep efficiency (%)",
                "hrv_ms": "HRV (ms)",
                "resting_hr_bpm": "Resting heart rate (bpm)",
                "readiness_score": "Readiness score",
                "body_temperature_delta": "Body temp delta (°C)",
                "steps": "Steps",
                "active_calories_kcal": "Active calories (kcal)",
                "active_minutes": "Active minutes",
                "vo2_max": "VO2 max",
                "training_load": "Training load",
            }
            for attr, label in ctx_map.items():
                val = getattr(ctx, attr, None)
                if val is not None:
                    lines.append(f"  {label}: {val}")
            for key, val in ctx.extras.items():
                lines.append(f"  {key}: {val}")

    # Historical trend data
    if history:
        lines.append(f"\n## Recent history ({len(history)} sessions)")
        lines.append("date, weight_kg, body_fat_pct, muscle_mass_kg, phase_angle")
        for h in history[-20:]:  # last 20 sessions in prompt
            row = [
                h.get("recorded_at", "")[:10],
                f"{h.get('weight_kg', ''):.1f}" if h.get("weight_kg") else "-",
                f"{h.get('body_fat_pct', ''):.1f}" if h.get("body_fat_pct") else "-",
                f"{h.get('muscle_mass_kg', ''):.1f}" if h.get("muscle_mass_kg") else "-",
                f"{h.get('phase_angle', ''):.1f}" if h.get("phase_angle") else "-",
            ]
            lines.append(", ".join(row))

    # Wearable history summary
    if wearable_history:
        lines.append("\n## Recent wearable averages (last 14 days)")
        for source, metrics in wearable_history.items():
            lines.append(f"\n{source}:")
            for key, values in metrics.items():
                if values:
                    avg = sum(v for _, v in values) / len(values)
                    lines.append(f"  avg {key}: {avg:.1f}")

    lines += [
        "",
        "## Your task",
        "Respond with a JSON object with exactly these three keys:",
        '  "summary": 2-3 sentence overview of today\'s measurement and notable changes',
        '  "trends": 2-3 sentence analysis of trends over the available history',
        '  "recommendations": 2-3 specific, data-driven action items for the next week',
        "",
        "Important: Keep each value under 200 words. Be specific about numbers.",
        "Do not include generic health disclaimers.",
        "Respond ONLY with the JSON object, no other text.",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# API call
# ---------------------------------------------------------------------------

async def _call_claude(prompt: str, model: str, api_key: str) -> str:
    """Call the Anthropic Messages API and return the raw text response."""
    import httpx

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": prompt}],
                "system": (
                    "You are a precise health data analyst. "
                    "Always respond with valid JSON only, no markdown, no preamble."
                ),
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["content"][0]["text"]


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _parse_response(raw: str) -> dict:
    """
    Extract the JSON object from Claude's response.
    Handles cases where Claude wraps the JSON in markdown code fences.
    """
    text = raw.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        parsed = json.loads(text)
        return {
            "summary": str(parsed.get("summary", "")),
            "trends": str(parsed.get("trends", "")),
            "recommendations": str(parsed.get("recommendations", "")),
        }
    except json.JSONDecodeError:
        logger.warning("Could not parse AI response as JSON — storing as summary")
        return {"summary": raw[:500], "trends": "", "recommendations": ""}


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def _get_profile_id(db: Database, session_id: int) -> int:
    cur = await db._conn.execute(
        "SELECT profile_id FROM sessions WHERE id=?", (session_id,)
    )
    row = await cur.fetchone()
    return row["profile_id"] if row else 1


async def _build_history(db: Database, profile_id: int, days: int) -> list[dict]:
    return await db.get_sessions(profile_id, limit=days)


async def _build_wearable_history(
    db: Database, profile_id: int, days: int
) -> dict[str, dict[str, list]]:
    """Fetch recent wearable metrics grouped by source and key."""
    metrics_of_interest = [
        ("sleep_score", None),
        ("hrv_ms", None),
        ("resting_hr_bpm", None),
        ("readiness_score", None),
        ("steps", None),
    ]
    result: dict[str, dict] = {}

    available = await db.get_available_metrics(profile_id)
    for source, keys in available.get("wearables", {}).items():
        result[source] = {}
        for key in keys:
            if key in [m[0] for m in metrics_of_interest]:
                history = await db.get_context_history(profile_id, source, key, days=14)
                if history:
                    result[source][key] = history

    return result
