from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from backend.asr.config import (
    AudioConfig,
    AudioTriggerConfig,
    BrowserConstraintsConfig,
    RecordingConfig,
    VadConfig,
)


FRAME_SAMPLES = 320
SAMPLE_RATE = 16000


def audio_config() -> AudioConfig:
    return AudioConfig(
        sample_rate_hz=16000,
        channels=1,
        sample_format="float32",
        capture_frame_ms=20,
        input_gain_db=12.0,
        input_gain_min_db=-24.0,
        input_gain_max_db=36.0,
        input_gain_step_db=0.5,
        clip_limit=0.999,
        browser_constraints=BrowserConstraintsConfig(
            echo_cancellation=False,
            noise_suppression=False,
            auto_gain_control=False,
        ),
    )


def recording_config(
    *,
    pretrigger_ms: int = 40,
    tail_ms: int = 20,
    max_ms: int = 1000,
    min_ms: int = 20,
    candidate_ms: int = 60,
    end_no_voice_ms: int = 40,
    trim_pad_ms: int | None = None,
) -> RecordingConfig:
    return RecordingConfig(
        pretrigger_buffer_ms=pretrigger_ms,
        posttrigger_tail_ms=tail_ms,
        max_utterance_ms=max_ms,
        min_utterance_ms=min_ms,
        candidate_confirmation_timeout_ms=candidate_ms,
        end_no_voice_duration_ms=end_no_voice_ms,
        end_no_voice_trim_pad_ms=(
            end_no_voice_ms if trim_pad_ms is None else trim_pad_ms
        ),
        prevent_segment_overlap=True,
    )


def trigger_config(
    *,
    min_trigger_ms: int = 40,
    enabled: bool = True,
    require_vad_confirmation: bool = True,
) -> AudioTriggerConfig:
    return AudioTriggerConfig(
        enabled=enabled,
        metric="rms_dbfs",
        start_threshold_dbfs=-40.0,
        min_trigger_duration_ms=min_trigger_ms,
        require_vad_confirmation=require_vad_confirmation,
    )


def vad_config(*, speech_pad_ms: int = 30, min_silence_ms: int = 32) -> VadConfig:
    return VadConfig(
        engine="silero",
        version="6.2",
        backend="onnx",
        model_path="package://silero_vad/data/silero_vad.onnx",
        device="cpu",
        threshold=0.5,
        min_silence_duration_ms=min_silence_ms,
        speech_pad_ms=speech_pad_ms,
        frame_samples=512,
        compute_gating_enabled=False,
        compute_wake_threshold_dbfs=-35.0,
        compute_suspend_after_quiet_ms=500,
        compute_warmup_context_ms=100,
    )


def build_machine(
    *,
    pretrigger_ms: int = 40,
    tail_ms: int = 20,
    max_ms: int = 1000,
    min_ms: int = 20,
    candidate_ms: int = 60,
    end_no_voice_ms: int = 40,
    trim_pad_ms: int | None = None,
    min_trigger_ms: int = 40,
    speech_pad_ms: int = 30,
    last_committed_sample_index: int = 0,
    trigger_enabled: bool = True,
    require_vad_confirmation: bool = True,
):
    from backend.asr.state_machine import RecordingStateMachine

    return RecordingStateMachine(
        audio_config=audio_config(),
        recording_config=recording_config(
            pretrigger_ms=pretrigger_ms,
            tail_ms=tail_ms,
            max_ms=max_ms,
            min_ms=min_ms,
            candidate_ms=candidate_ms,
            end_no_voice_ms=end_no_voice_ms,
            trim_pad_ms=trim_pad_ms,
        ),
        trigger_config=trigger_config(
            min_trigger_ms=min_trigger_ms,
            enabled=trigger_enabled,
            require_vad_confirmation=require_vad_confirmation,
        ),
        vad_config=vad_config(speech_pad_ms=speech_pad_ms),
        last_committed_sample_index=last_committed_sample_index,
    )


def indexed_frame(start: int):
    from backend.asr.models import IndexedAudioFrame

    samples = np.arange(start, start + FRAME_SAMPLES, dtype=np.float32)
    return IndexedAudioFrame(samples, start, start + FRAME_SAMPLES)


def feed_frame(machine, index: int, *, dbfs: float, probability: float, speech: bool):
    start = index * FRAME_SAMPLES
    return machine.accept(
        indexed_frame(start),
        dbfs=dbfs,
        vad_probability=probability,
        vad_is_speech=speech,
    )


def completed(events):
    return [event.utterance for event in events if event.kind == "completed"]


@dataclass
class FakeProbability:
    value: float

    def item(self) -> float:
        return self.value


class FakeSileroModel:
    def __init__(self, probabilities: list[float]) -> None:
        self.probabilities = iter(probabilities)
        self.chunk_sizes: list[int] = []
        self.reset_count = 0

    def __call__(self, samples, sample_rate_hz):
        assert sample_rate_hz == SAMPLE_RATE
        self.chunk_sizes.append(int(np.asarray(samples).size))
        return FakeProbability(next(self.probabilities))

    def reset_states(self) -> None:
        self.reset_count += 1


def test_silero_adapter_aggregates_exact_chunks_and_tracks_speech_edges():
    from backend.asr.vad import SileroVadAdapter

    model = FakeSileroModel([0.8, 0.1])
    adapter = SileroVadAdapter(
        vad_config(speech_pad_ms=0), sample_rate_hz=SAMPLE_RATE, model=model
    )

    assert adapter.accept(np.zeros(300, np.float32)) == []
    observations = adapter.accept(np.zeros(724, np.float32))

    assert model.chunk_sizes == [512, 512]
    assert observations[0].probability == 0.8
    assert observations[0].speech_started is True
    assert observations[0].is_speech is True
    assert observations[1].speech_ended is True
    assert observations[1].is_speech is False


def test_silero_reset_clears_partial_chunk_and_model_state():
    from backend.asr.vad import SileroVadAdapter

    model = FakeSileroModel([0.7])
    adapter = SileroVadAdapter(vad_config(), sample_rate_hz=SAMPLE_RATE, model=model)
    adapter.accept(np.zeros(300, np.float32))

    adapter.reset()
    assert adapter.accept(np.zeros(212, np.float32)) == []
    assert model.chunk_sizes == []
    assert model.reset_count == 1


def test_silero_speech_pad_delays_end_event_by_configured_chunks():
    from backend.asr.vad import SileroVadAdapter

    no_pad_model = FakeSileroModel([0.8, 0.1])
    no_pad = SileroVadAdapter(
        vad_config(speech_pad_ms=0, min_silence_ms=32),
        sample_rate_hz=SAMPLE_RATE,
        model=no_pad_model,
    )
    no_pad_observations = no_pad.accept(np.zeros(1024, np.float32))

    padded_model = FakeSileroModel([0.8, 0.1, 0.1, 0.1])
    padded = SileroVadAdapter(
        vad_config(speech_pad_ms=64, min_silence_ms=32),
        sample_rate_hz=SAMPLE_RATE,
        model=padded_model,
    )
    padded_observations = padded.accept(np.zeros(2048, np.float32))

    assert no_pad_observations[1].speech_ended is True
    assert no_pad_observations[1].is_speech is False
    assert [observation.is_speech for observation in padded_observations] == [
        True,
        True,
        True,
        False,
    ]
    assert [observation.speech_ended for observation in padded_observations] == [
        False,
        False,
        False,
        True,
    ]


def test_silero_reset_clears_active_speech_padding_state():
    from backend.asr.vad import SileroVadAdapter

    model = FakeSileroModel([0.8, 0.1, 0.1])
    adapter = SileroVadAdapter(
        vad_config(speech_pad_ms=64, min_silence_ms=32),
        sample_rate_hz=SAMPLE_RATE,
        model=model,
    )
    before_reset = adapter.accept(np.zeros(1024, np.float32))
    assert before_reset[-1].is_speech is True

    adapter.reset()
    after_reset = adapter.accept(np.zeros(512, np.float32))[0]

    assert after_reset.is_speech is False
    assert after_reset.speech_ended is False
    assert model.reset_count == 1


def test_recent_audio_snapshot_returns_requested_tail():
    machine = build_machine(pretrigger_ms=60)
    for index in range(5):
        feed_frame(machine, index, dbfs=-20.0, probability=0.8, speech=True)

    snippet = machine.recent_audio_samples(40)
    np.testing.assert_array_equal(
        snippet,
        np.arange(3 * FRAME_SAMPLES, 5 * FRAME_SAMPLES, dtype=np.float32),
    )


def test_recent_audio_snapshot_clamps_to_buffered_audio():
    machine = build_machine(pretrigger_ms=60)
    for index in range(5):
        feed_frame(machine, index, dbfs=-20.0, probability=0.8, speech=True)

    snippet = machine.recent_audio_samples(10_000)
    np.testing.assert_array_equal(
        snippet,
        np.arange(640, 5 * FRAME_SAMPLES, dtype=np.float32),
    )


def test_no_voice_timeout_trims_verified_silence_but_keeps_pad():
    machine = build_machine(
        pretrigger_ms=0,
        min_trigger_ms=20,
        end_no_voice_ms=100,
        trim_pad_ms=40,
        tail_ms=0,
    )
    events = []
    events.extend(feed_frame(machine, 0, dbfs=-20.0, probability=0.8, speech=True))
    for index in range(1, 6):
        events.extend(
            feed_frame(machine, index, dbfs=-60.0, probability=0.1, speech=False)
        )

    utterance = completed(events)[0]
    assert utterance is not None
    kept = 3 * FRAME_SAMPLES
    assert utterance.samples.size == kept
    assert utterance.end_sample_index == utterance.start_sample_index + kept
    np.testing.assert_array_equal(
        utterance.samples,
        np.arange(utterance.start_sample_index, utterance.end_sample_index, dtype=np.float32),
    )
    assert utterance.metadata.silero_end_reason == "no_voice_timeout"
    assert utterance.metadata.duration_ms == kept * 1000.0 / SAMPLE_RATE


def test_mid_window_voice_resumes_full_audio_without_trim():
    machine = build_machine(
        pretrigger_ms=0,
        min_trigger_ms=20,
        end_no_voice_ms=60,
        trim_pad_ms=0,
        tail_ms=0,
    )
    events = []
    events.extend(feed_frame(machine, 0, dbfs=-20.0, probability=0.8, speech=True))
    events.extend(feed_frame(machine, 1, dbfs=-60.0, probability=0.1, speech=False))
    events.extend(feed_frame(machine, 2, dbfs=-60.0, probability=0.1, speech=False))
    events.extend(feed_frame(machine, 3, dbfs=-20.0, probability=0.8, speech=True))
    for index in range(4, 7):
        events.extend(
            feed_frame(machine, index, dbfs=-60.0, probability=0.1, speech=False)
        )

    utterance = completed(events)[0]
    assert utterance is not None
    assert utterance.samples.size == 4 * FRAME_SAMPLES
    np.testing.assert_array_equal(
        utterance.samples,
        np.arange(0, 4 * FRAME_SAMPLES, dtype=np.float32),
    )


def test_min_utterance_check_uses_untrimmed_recording_length():
    machine = build_machine(
        pretrigger_ms=0,
        min_trigger_ms=20,
        end_no_voice_ms=100,
        trim_pad_ms=0,
        tail_ms=0,
        min_ms=100,
    )
    events = []
    events.extend(feed_frame(machine, 0, dbfs=-20.0, probability=0.8, speech=True))
    for index in range(1, 6):
        events.extend(
            feed_frame(machine, index, dbfs=-60.0, probability=0.1, speech=False)
        )

    utterances = completed(events)
    assert len(utterances) == 1
    assert [event.reason for event in events if event.kind == "rejected"] == []
    assert utterances[0] is not None
    assert utterances[0].samples.size == FRAME_SAMPLES


def test_pretrigger_trigger_frame_and_tail_are_each_present_once():
    from backend.asr.models import RecordingState

    machine = build_machine(pretrigger_ms=40, end_no_voice_ms=40, tail_ms=20)
    events = []
    scenario = [
        (-60.0, 0.1, False),
        (-60.0, 0.1, False),
        (-20.0, 0.1, False),
        (-20.0, 0.8, True),
        (-60.0, 0.1, False),
        (-60.0, 0.1, False),
        (-60.0, 0.1, False),
    ]
    for index, (dbfs, probability, speech) in enumerate(scenario):
        events.extend(
            feed_frame(
                machine,
                index,
                dbfs=dbfs,
                probability=probability,
                speech=speech,
            )
        )

    utterances = completed(events)
    assert len(utterances) == 1
    utterance = utterances[0]
    assert utterance is not None
    assert utterance.pretrigger_samples == 640
    np.testing.assert_array_equal(
        utterance.samples,
        np.arange(
            utterance.start_sample_index,
            utterance.end_sample_index,
            dtype=np.float32,
        ),
    )
    transitions = [event.state for event in events if event.kind == "state_changed"]
    assert transitions == [
        RecordingState.CANDIDATE,
        RecordingState.RECORDING,
        RecordingState.FINALIZING,
    ]
    assert machine.state is RecordingState.FINALIZING


def test_pretrigger_duration_is_independent_from_vad_speech_pad():
    machine = build_machine(
        pretrigger_ms=40,
        speech_pad_ms=5,
        min_trigger_ms=20,
        end_no_voice_ms=20,
        tail_ms=0,
    )
    events = []
    for index, values in enumerate(
        [
            (-60.0, 0.1, False),
            (-60.0, 0.1, False),
            (-20.0, 0.8, True),
            (-60.0, 0.1, False),
        ]
    ):
        events.extend(
            feed_frame(
                machine,
                index,
                dbfs=values[0],
                probability=values[1],
                speech=values[2],
            )
        )

    utterance = completed(events)[0]
    assert utterance is not None
    assert utterance.pretrigger_samples == 640


def test_end_timer_tracks_vad_no_voice_frames_regardless_of_level():
    machine = build_machine(min_trigger_ms=20, end_no_voice_ms=60)
    feed_frame(machine, 0, dbfs=-20.0, probability=0.8, speech=True)

    feed_frame(machine, 1, dbfs=-60.0, probability=0.8, speech=True)
    assert machine.end_no_voice_samples == 0
    feed_frame(machine, 2, dbfs=-20.0, probability=0.1, speech=False)
    assert machine.end_no_voice_samples == FRAME_SAMPLES
    feed_frame(machine, 3, dbfs=-60.0, probability=0.1, speech=True)
    assert machine.end_no_voice_samples == 2 * FRAME_SAMPLES
    feed_frame(machine, 4, dbfs=-20.0, probability=0.8, speech=False)
    assert machine.end_no_voice_samples == 0


def test_vad_only_trigger_ignores_dbfs_and_honors_minimum_duration():
    from backend.asr.models import RecordingState

    machine = build_machine(
        min_trigger_ms=40,
        trigger_enabled=False,
        require_vad_confirmation=True,
    )

    first = feed_frame(
        machine, 0, dbfs=-100.0, probability=0.8, speech=False
    )
    second = feed_frame(
        machine, 1, dbfs=-100.0, probability=0.8, speech=False
    )

    assert first == []
    assert [event.state for event in second if event.kind == "state_changed"] == [
        RecordingState.CANDIDATE,
        RecordingState.RECORDING,
    ]
    assert machine.state is RecordingState.RECORDING


def test_vad_only_end_uses_probability_even_while_padded_speech_is_true():
    machine = build_machine(
        min_trigger_ms=20,
        end_no_voice_ms=40,
        tail_ms=0,
        trigger_enabled=False,
    )
    events = []
    events.extend(
        feed_frame(machine, 0, dbfs=-20.0, probability=0.8, speech=True)
    )
    events.extend(
        feed_frame(machine, 1, dbfs=-20.0, probability=0.1, speech=True)
    )
    assert machine.end_no_voice_samples == FRAME_SAMPLES
    events.extend(
        feed_frame(machine, 2, dbfs=-20.0, probability=0.1, speech=True)
    )

    assert len(completed(events)) == 1


def test_loud_noise_with_no_vad_voice_ends_recording():
    machine = build_machine(
        min_trigger_ms=20,
        end_no_voice_ms=60,
        tail_ms=0,
        trigger_enabled=True,
    )
    feed_frame(machine, 0, dbfs=-20.0, probability=0.8, speech=True)
    events = []
    for index in range(1, 4):
        events.extend(
            feed_frame(machine, index, dbfs=-20.0, probability=0.1, speech=False)
        )

    utterances = completed(events)
    assert len(utterances) == 1
    assert utterances[0] is not None
    assert utterances[0].metadata.silero_end_reason == "no_voice_timeout"


def test_candidate_timeout_rejects_without_completion():
    from backend.asr.models import RecordingState

    machine = build_machine(
        min_trigger_ms=20,
        candidate_ms=40,
        end_no_voice_ms=20,
        tail_ms=0,
    )
    events = []
    for index in range(3):
        events.extend(
            feed_frame(
                machine,
                index,
                dbfs=-20.0,
                probability=0.1,
                speech=False,
            )
        )

    assert completed(events) == []
    assert any(event.kind == "rejected" for event in events)
    assert machine.state is RecordingState.BUFFERING


def test_candidate_confirmation_at_max_boundary_uses_frame_only_as_tail():
    from backend.asr.models import RecordingState

    machine = build_machine(
        pretrigger_ms=0,
        min_trigger_ms=20,
        candidate_ms=999,
        max_ms=1000,
        min_ms=20,
        end_no_voice_ms=40,
        tail_ms=20,
    )
    events = []
    for index in range(50):
        events.extend(
            feed_frame(
                machine,
                index,
                dbfs=-20.0,
                probability=0.1,
                speech=False,
            )
        )
    events.extend(
        feed_frame(
            machine,
            50,
            dbfs=-20.0,
            probability=0.8,
            speech=True,
        )
    )

    utterances = completed(events)
    assert len(utterances) == 1
    utterance = utterances[0]
    assert utterance is not None
    assert utterance.samples.size == 51 * FRAME_SAMPLES
    assert utterance.start_sample_index == 0
    assert utterance.end_sample_index == 51 * FRAME_SAMPLES
    assert utterance.metadata.max_duration_reached is True
    np.testing.assert_array_equal(
        utterance.samples,
        np.arange(51 * FRAME_SAMPLES, dtype=np.float32),
    )
    assert [event.state for event in events if event.kind == "state_changed"] == [
        RecordingState.CANDIDATE,
        RecordingState.RECORDING,
        RecordingState.FINALIZING,
    ]


def test_subframe_max_immediately_confirmed_trigger_splits_body_and_tail():
    from backend.asr.models import RecordingState

    machine = build_machine(
        pretrigger_ms=0,
        min_trigger_ms=20,
        candidate_ms=10,
        max_ms=15,
        min_ms=5,
        end_no_voice_ms=10,
        tail_ms=5,
    )

    events = feed_frame(
        machine,
        0,
        dbfs=-20.0,
        probability=0.8,
        speech=True,
    )

    utterances = completed(events)
    assert len(utterances) == 1
    utterance = utterances[0]
    assert utterance is not None
    assert utterance.samples.size == FRAME_SAMPLES
    assert utterance.start_sample_index == 0
    assert utterance.end_sample_index == FRAME_SAMPLES
    assert utterance.metadata.recorded_samples == FRAME_SAMPLES
    assert utterance.metadata.max_duration_reached is True
    np.testing.assert_array_equal(
        utterance.samples,
        np.arange(FRAME_SAMPLES, dtype=np.float32),
    )
    assert [event.state for event in events if event.kind == "state_changed"] == [
        RecordingState.CANDIDATE,
        RecordingState.RECORDING,
        RecordingState.FINALIZING,
    ]


def test_subframe_max_unconfirmed_trigger_retains_order_until_confirmation():
    from backend.asr.models import RecordingState

    machine = build_machine(
        pretrigger_ms=0,
        min_trigger_ms=20,
        candidate_ms=10,
        max_ms=15,
        min_ms=5,
        end_no_voice_ms=10,
        tail_ms=5,
    )
    events = feed_frame(
        machine,
        0,
        dbfs=-20.0,
        probability=0.1,
        speech=False,
    )

    assert completed(events) == []
    assert machine.state is RecordingState.CANDIDATE

    events.extend(
        feed_frame(
            machine,
            1,
            dbfs=-20.0,
            probability=0.8,
            speech=True,
        )
    )

    utterances = completed(events)
    assert len(utterances) == 1
    utterance = utterances[0]
    assert utterance is not None
    assert utterance.samples.size == FRAME_SAMPLES
    assert utterance.start_sample_index == 0
    assert utterance.end_sample_index == FRAME_SAMPLES
    assert utterance.metadata.recorded_samples == FRAME_SAMPLES
    assert utterance.metadata.max_duration_reached is True
    np.testing.assert_array_equal(
        utterance.samples,
        np.arange(FRAME_SAMPLES, dtype=np.float32),
    )
    assert [event.state for event in events if event.kind == "state_changed"] == [
        RecordingState.CANDIDATE,
        RecordingState.RECORDING,
        RecordingState.FINALIZING,
    ]


def test_candidate_timeout_boundary_rejects_then_preserves_ordered_recovery():
    from backend.asr.models import RecordingState

    machine = build_machine(
        pretrigger_ms=40,
        min_trigger_ms=20,
        candidate_ms=999,
        max_ms=1000,
        min_ms=20,
        end_no_voice_ms=40,
        tail_ms=20,
    )
    events = []
    for index in range(51):
        events.extend(
            feed_frame(
                machine,
                index,
                dbfs=-20.0,
                probability=0.1,
                speech=False,
            )
        )

    rejected = [event for event in events if event.kind == "rejected"]
    assert len(rejected) == 1
    assert rejected[0].reason == "candidate_timeout"
    assert completed(events) == []
    assert machine.state is RecordingState.BUFFERING

    recovery = [
        (-60.0, 0.1, False),
        (-20.0, 0.8, True),
        (-60.0, 0.1, False),
        (-60.0, 0.1, False),
        (-60.0, 0.1, False),
    ]
    for index, (dbfs, probability, speech) in enumerate(recovery, start=51):
        events.extend(
            feed_frame(
                machine,
                index,
                dbfs=dbfs,
                probability=probability,
                speech=speech,
            )
        )

    utterance = completed(events)[0]
    assert utterance is not None
    assert utterance.pretrigger_samples == 2 * FRAME_SAMPLES
    assert utterance.start_sample_index == 50 * FRAME_SAMPLES
    assert utterance.end_sample_index == 56 * FRAME_SAMPLES
    np.testing.assert_array_equal(
        utterance.samples,
        np.arange(50 * FRAME_SAMPLES, 56 * FRAME_SAMPLES, dtype=np.float32),
    )


def test_short_utterance_is_rejected_after_finalization():
    machine = build_machine(
        min_trigger_ms=20,
        min_ms=60,
        end_no_voice_ms=20,
        tail_ms=0,
    )
    events = []
    events.extend(feed_frame(machine, 0, dbfs=-20.0, probability=0.8, speech=True))
    events.extend(feed_frame(machine, 1, dbfs=-60.0, probability=0.1, speech=False))

    assert completed(events) == []
    assert [event.reason for event in events if event.kind == "rejected"] == [
        "too_short"
    ]


def test_max_duration_forces_exactly_one_completion():
    machine = build_machine(
        pretrigger_ms=0,
        min_trigger_ms=20,
        max_ms=100,
        min_ms=20,
        candidate_ms=40,
        end_no_voice_ms=40,
        tail_ms=20,
    )
    events = []
    for index in range(7):
        events.extend(
            feed_frame(
                machine,
                index,
                dbfs=-20.0,
                probability=0.8,
                speech=True,
            )
        )

    utterances = completed(events)
    assert len(utterances) == 1
    assert utterances[0] is not None
    assert utterances[0].metadata.max_duration_reached is True
    assert utterances[0].samples.size == 6 * FRAME_SAMPLES
    np.testing.assert_array_equal(
        utterances[0].samples,
        np.arange(6 * FRAME_SAMPLES, dtype=np.float32),
    )
    completion_index = next(
        index for index, event in enumerate(events) if event.kind == "completed"
    )
    assert events[completion_index - 1].state.value == "finalizing"


def test_pretrigger_larger_than_max_body_remains_ordered_and_bounded():
    machine = build_machine(
        pretrigger_ms=2000,
        min_trigger_ms=20,
        max_ms=1000,
        min_ms=20,
        candidate_ms=40,
        end_no_voice_ms=40,
        tail_ms=20,
    )
    events = []
    for index in range(100):
        events.extend(
            feed_frame(
                machine,
                index,
                dbfs=-60.0,
                probability=0.1,
                speech=False,
            )
        )
    for index in range(100, 151):
        events.extend(
            feed_frame(
                machine,
                index,
                dbfs=-20.0,
                probability=0.8,
                speech=True,
            )
        )

    utterance = completed(events)[0]
    assert utterance is not None
    assert utterance.pretrigger_samples == 2000 * SAMPLE_RATE // 1000
    assert utterance.metadata.recorded_samples == (1000 + 20) * SAMPLE_RATE // 1000
    assert utterance.start_sample_index == 0
    assert utterance.end_sample_index == 151 * FRAME_SAMPLES
    np.testing.assert_array_equal(
        utterance.samples,
        np.arange(151 * FRAME_SAMPLES, dtype=np.float32),
    )


def test_tail_never_exceeds_non_frame_aligned_configured_duration():
    machine = build_machine(
        pretrigger_ms=0,
        min_trigger_ms=20,
        min_ms=20,
        end_no_voice_ms=20,
        tail_ms=10,
    )
    events = []
    events.extend(feed_frame(machine, 0, dbfs=-20.0, probability=0.8, speech=True))
    events.extend(feed_frame(machine, 1, dbfs=-60.0, probability=0.1, speech=False))
    events.extend(feed_frame(machine, 2, dbfs=-60.0, probability=0.1, speech=False))

    utterance = completed(events)[0]
    assert utterance is not None
    assert utterance.samples.size == 2 * FRAME_SAMPLES + 160
    assert utterance.end_sample_index == 2 * FRAME_SAMPLES + 160


def test_max_duration_truncates_at_exact_non_frame_aligned_boundary():
    machine = build_machine(
        pretrigger_ms=0,
        min_trigger_ms=20,
        max_ms=90,
        min_ms=20,
        candidate_ms=40,
        end_no_voice_ms=40,
        tail_ms=20,
    )
    events = []
    for index in range(6):
        events.extend(
            feed_frame(
                machine,
                index,
                dbfs=-20.0,
                probability=0.8,
                speech=True,
            )
        )

    utterance = completed(events)[0]
    assert utterance is not None
    assert utterance.samples.size == 1760
    assert utterance.end_sample_index == 1760
    assert utterance.metadata.max_duration_reached is True


def test_only_explicit_success_advances_committed_boundary():
    machine = build_machine(
        pretrigger_ms=0,
        min_trigger_ms=20,
        max_ms=100,
        min_ms=20,
        candidate_ms=40,
        end_no_voice_ms=40,
        tail_ms=20,
        last_committed_sample_index=100,
    )
    events = []
    for index in range(6):
        start = 100 + index * FRAME_SAMPLES
        events.extend(
            machine.accept(
                indexed_frame(start),
                dbfs=-20.0,
                vad_probability=0.8,
                vad_is_speech=True,
            )
        )

    utterance = completed(events)[0]
    assert utterance is not None
    assert machine.last_committed_sample_index == 100

    machine.mark_committed(utterance)
    assert machine.last_committed_sample_index == utterance.end_sample_index


def _consecutive_utterances():
    first_machine = build_machine(
        pretrigger_ms=0,
        min_trigger_ms=20,
        end_no_voice_ms=20,
        tail_ms=0,
    )
    first_events = []
    first_events.extend(
        feed_frame(
            first_machine, 0, dbfs=-20.0, probability=0.8, speech=True
        )
    )
    first_events.extend(
        feed_frame(
            first_machine, 1, dbfs=-60.0, probability=0.1, speech=False
        )
    )
    first = completed(first_events)[0]
    assert first is not None
    first_machine.mark_committed(first)

    second_machine = build_machine(
        pretrigger_ms=40,
        min_trigger_ms=20,
        end_no_voice_ms=20,
        tail_ms=0,
        last_committed_sample_index=first.end_sample_index,
    )
    second_events = []
    for index, values in (
        (1, (-60.0, 0.1, False)),
        (2, (-60.0, 0.1, False)),
        (3, (-20.0, 0.8, True)),
        (4, (-60.0, 0.1, False)),
    ):
        second_events.extend(
            feed_frame(
                second_machine,
                index,
                dbfs=values[0],
                probability=values[1],
                speech=values[2],
            )
        )
    second = completed(second_events)[0]
    assert second is not None
    return first, second


def test_prevent_segment_overlap_crops_second_utterance_at_commit_boundary():
    first, second = _consecutive_utterances()

    assert first.end_sample_index == 2 * FRAME_SAMPLES
    assert second.start_sample_index == first.end_sample_index
    np.testing.assert_array_equal(
        second.samples,
        np.arange(
            second.start_sample_index,
            second.end_sample_index,
            dtype=np.float32,
        ),
    )
