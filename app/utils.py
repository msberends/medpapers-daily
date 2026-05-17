"""Shared utility functions used across multiple route modules."""
import json


def classify_paper(mesh_terms_json: str, mesh_topic_map: dict,
                   keywords_json: str = "[]") -> list[str]:
    """Return sorted list of topic labels for a paper based on the user's mesh_topic_map."""
    terms = json.loads(mesh_terms_json or "[]") + json.loads(keywords_json or "[]")
    topics = {mesh_topic_map[t.lower()] for t in terms if t.lower() in mesh_topic_map}
    return sorted(topics)


def get_relevance_cfg(user_yaml: dict) -> dict:
    """Return the relevance alert configuration from a user YAML dict."""
    return {
        "enabled": user_yaml.get("relevance_alert_enabled", True),
        "threshold": user_yaml.get("relevance_alert_threshold", 0.30),
        "min_rated": user_yaml.get("relevance_alert_min_rated", 10),
        "lookback_days": user_yaml.get("relevance_alert_lookback_days", 30),
    }
