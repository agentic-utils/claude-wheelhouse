"""The models and effort levels a session can run on (.plan/model-effort.md): what the New
session dialog, the Model action, the effort slider, the store's checks and the daemon's
GET /models all offer. Each model is a pinned full id, so a relaunch never changes model by
itself; a new model is a row here."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Model:
    id: str        # the pinned id passed to --model and set_model
    name: str      # as the person reads it
    effort: str    # its default effort


MODELS = (Model("claude-opus-5-5", "Opus 5.5", "high"),
          Model("claude-sonnet-5-5", "Sonnet 5.5", "high"),
          Model("claude-haiku-5-5", "Haiku 5.5", "xhigh"),
          Model("claude-fable-5-1", "Fable 5.1", "high"))
BY_ID = {m.id: m for m in MODELS}
EFFORTS = ("low", "medium", "high", "xhigh", "max")   # every model offers every level
DEFAULT_MODEL = "claude-sonnet-5-5"


def check_model(model: str) -> str:
    if model not in BY_ID:
        raise ValueError(f"model must be one of {', '.join(BY_ID)}, not {model!r}")
    return model


def check_effort(effort: str) -> str:
    if effort not in EFFORTS:
        raise ValueError(f"effort must be one of {', '.join(EFFORTS)}, not {effort!r}")
    return effort


def default_effort(model: str) -> str:
    return BY_ID[model].effort if model in BY_ID else "high"


def name(model: str | None) -> str | None:
    """A model's display name: the table's, else None (an id from elsewhere). A transcript's
    requested model may carry a suffix (claude-opus-5-5[1m]): it is the same model."""
    base = (model or "").split("[")[0]
    return BY_ID[base].name if base in BY_ID else None


def pinned(model: str) -> str:
    """A model id as a launch passes it: a table model's suffixed id (claude-opus-5-5[1m],
    as a transcript records a /model opus[1m]) as its pinned id, which is the same model with
    the same 1M window; any other id as it is."""
    base = model.split("[")[0]
    return base if base in BY_ID else model


def chosen(model: str | None, effort: str | None) -> tuple[str, str]:
    """A new session's model and effort: as given, else the default model and that model's
    default effort. Either given is checked against the table."""
    model = check_model(model) if model is not None else DEFAULT_MODEL
    return model, check_effort(effort) if effort is not None else default_effort(model)


def catalogue() -> dict:
    """The table, for a client building the same choices (GET /models)."""
    return {"models": [{"id": m.id, "name": m.name, "effort": m.effort} for m in MODELS],
            "efforts": list(EFFORTS), "default": DEFAULT_MODEL}


def field(session, key: str):
    """A column a row may not have: one from older code's view, or a plain dict standing
    in for a row (launch.injected)."""
    return session[key] if key in session.keys() else None
