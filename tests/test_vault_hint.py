"""The vault hint: a fixed note on the first health turn of a session, only when the vault holds data."""
import pytest

from conftest import FakeCtx, load_plugin_package
from caduceus import hint
from caduceus.core import search
from caduceus.core.db import write_txn

OZEMPIC = "Why isn't my Ozempic working?"


@pytest.fixture(autouse=True)
def fresh_hint_state():
    hint._reset()
    yield
    hint._reset()


@pytest.fixture
def filled(vault):
    with write_txn(vault):
        search.index_doc(vault, kind="journal", ref="1", title="Note", body="felt fine", category="wellness")
    return vault


def add_medication(vault, drug, brand=None):
    with write_txn(vault):
        vault.execute("INSERT INTO medication_event(drug, brand, event, occurred_on, created_at) "
                      "VALUES (?, ?, 'start', '2026-01-01', '2026-01-01T00:00:00Z')", (drug, brand))


def ask(text, session="s1", history=None, ctx=None):
    return hint.pre_llm_call(ctx or FakeCtx(), session_id=session, user_message=text,
                             conversation_history=history or [], platform="cli", telemetry_schema_version=1)


def test_no_vault_or_empty_vault_gives_no_hint(data_dir):
    assert ask(OZEMPIC) is None
    assert not (data_dir / "vault.db").exists()  # the hook never creates the vault
    from caduceus.core.db import open_vault

    open_vault(data_dir / "vault.db").close()
    hint._reset()
    assert ask(OZEMPIC) is None


def test_first_health_turn_of_each_session_gets_the_fixed_note(filled):
    assert ask(OZEMPIC, "s1") == {"context": hint.HINT}
    assert ask("And would Mounjaro be different?", "s1") is None
    assert ask(OZEMPIC, "s2") == {"context": hint.HINT}


def test_note_in_history_counts_after_a_restart(filled):
    earlier = [{"role": "user", "content": OZEMPIC, "api_content": OZEMPIC + "\n\n" + hint.HINT},
               {"role": "assistant", "content": "Your vault shows..."}]
    assert ask("What about my metformin?", "s1", earlier) is None
    hint._reset()
    quoted = [{"role": "assistant", "content": hint.HINT}]  # only user turns carry the note
    assert ask("What about my metformin?", "s2", quoted) == {"context": hint.HINT}
    hint._reset()
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    multimodal = [{"role": "user", "content": [image, {"type": "text", "text": OZEMPIC + "\n\n" + hint.HINT}]}]
    assert ask("What about my metformin?", None, multimodal) is None  # no session id: the history decides


def test_a_user_typing_the_tag_still_gets_the_note(filled):
    typed = "[Caduceus] Why isn't my Ozempic working?"
    current = [{"role": "user", "content": typed}]  # Hermes passes the current message in the history too
    assert ask(typed, "s1", current) == {"context": hint.HINT}


@pytest.mark.parametrize("text", [
    OZEMPIC,
    "Would Mounjaro be different for me than Ozempic?",
    "Should I stop taking metformin?",
    "How did I sleep last month?",
    "What does my APOE say?",
    "What's my genotype at rs6923761?",
    "Show my blood pressure since March",
    "Am I a carrier for anything?",
    "¿Por qué no me funciona Ozempic?",
    "Any side effects to watch for with tirzepatide?",
    "How many steps did I walk last week?",
    "How much weight have I lost since May?",
    "What did my labs say about my kidneys?",
])
def test_health_questions_get_the_note(filled, text):
    assert ask(text) == {"context": hint.HINT}


@pytest.mark.parametrize("text", [
    "hello",
    "Add a health check endpoint to the server",
    "Why does time.sleep block the event loop?",
    "Rename the enum variants",
    "Load the model weights from disk",
    "Explain dependency injection in FastAPI",
    "Write the weekly report",
    "Run the test steps again",
    "Mix 5 fun ideas for the launch",
    "Explain side effects in React",
    "What are my next steps for the release?",
    "Fix my health check endpoint",
    "Why does my time.sleep block?",
    "Show me the test steps",
    "Is my code healthy?",
    "Read me the tabloid headlines",
    "Fix the SQL injection in my code",
    "Start taking backups daily",
])
def test_other_messages_get_nothing(filled, text):
    assert ask(text) is None


def test_recorded_medication_names_count_but_dose_form_words_do_not(filled):
    add_medication(filled, "Lisinopril 10 MG Oral Tablet")
    add_medication(filled, "levothyroxine", "Synthroid")
    assert ask("Is lisinopril fine with grapefruit?", "a") == {"context": hint.HINT}
    assert ask("Synthroid question", "b") == {"context": hint.HINT}
    assert ask("Order the oral history tablets weekly", "c") is None
    assert hint._name_words("metFORMIN 500 MG Oral Tablet [Glucophage]") == {"metformin", "glucophage"}
    assert hint._name_words("Semaglutide 0.25 MG/DOSE Pen Injector") == {"semaglutide"}


def test_the_note_is_constant_and_holds_no_vault_values(filled):
    add_medication(filled, "Lisinopril 10 MG Oral Tablet")
    assert ask("Is my lisinopril dose right?") == {"context": hint.HINT}
    assert "lisinopril" not in hint.HINT.lower()
    assert "prescriber" in hint.HINT and hint.HINT.startswith("[Caduceus] ")


def test_setting_off_means_no_note(filled):
    assert ask(OZEMPIC, ctx=FakeCtx({"vault_hint": False})) is None
    assert ask(OZEMPIC, ctx=FakeCtx({"vault_hint": True})) == {"context": hint.HINT}


def test_multimodal_turns_use_their_text_parts(filled):
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    assert ask([image]) is None
    assert ask([{"type": "text", "text": OZEMPIC}, image]) == {"context": hint.HINT}


def test_long_pastes_are_read_at_both_ends(filled):
    paste = "lorem ipsum " * 20_000
    assert ask(paste + " Should I stop my metformin?", "a") == {"context": hint.HINT}
    assert ask("Should I stop my metformin? " + paste, "b") == {"context": hint.HINT}


def test_errors_mean_no_note_and_never_a_block(filled, data_dir, monkeypatch):
    class BrokenConfig(FakeCtx):
        def get_config(self, key, default=None):
            raise RuntimeError("config unavailable")

    assert ask(OZEMPIC, "a", ctx=BrokenConfig()) == {"context": hint.HINT}  # unreadable setting: default on
    monkeypatch.setattr(hint, "_vault_state", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert ask(OZEMPIC, "b") is None
    monkeypatch.undo()
    hint._reset()
    (data_dir / "vault.db").write_bytes(b"not a database" * 100)
    assert ask(OZEMPIC, "c") is None


def test_vault_state_is_cached_then_refreshed(vault, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(hint.time, "monotonic", lambda: clock[0])
    assert ask(OZEMPIC, "a") is None  # empty vault
    with write_txn(vault):
        search.index_doc(vault, kind="journal", ref="1", title="Note", body="ok", category="wellness")
    clock[0] += 10
    assert ask(OZEMPIC, "b") is None  # still the cached answer
    clock[0] += hint._TTL
    assert ask(OZEMPIC, "c") == {"context": hint.HINT}


def test_remembered_sessions_are_bounded(filled):
    for n in range(hint._SEEN_MAX + 50):
        assert ask(OZEMPIC, f"s{n}") == {"context": hint.HINT}
    assert len(hint._seen) == hint._SEEN_MAX


def test_registered_hook_passes_hermes_kwargs_through(filled):
    ctx = FakeCtx()
    load_plugin_package().register(ctx)
    (callback,) = ctx.hooks["pre_llm_call"]
    out = callback(session_id="reg-1", task_id="t", turn_id="u", user_message=OZEMPIC, conversation_history=[],
                   is_first_turn=True, model="m", platform="cli", parent_session_id="", sender_id="",
                   telemetry_schema_version=1)
    assert out == {"context": hint.HINT}
    assert callback(session_id="reg-1", user_message=OZEMPIC, conversation_history=[]) is None
