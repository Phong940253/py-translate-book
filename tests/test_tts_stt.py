"""Opt-in end-to-end TTS→STT check for duplicated short clauses.

The ch152 regression is audible, not textual: the TTS model spoke

    "Đi thôi, đi tắm thôi."

as "Đi thôi. Đi thôi. Đi tắm thôi. Đi tắm thôi."  A text-only test cannot see
that, so this test synthesizes the real audio and transcribes it with
faster-whisper.  It is deliberately opt-in because both models are large and
download/load slowly:

    set PYTB_TTS_STT=1
    python -m unittest tests.test_tts_stt

Optional environment variables:
    PYTB_TTS_VOICE              preset voice (default: Ngọc Huyền)
    PYTB_WHISPER_MODEL          faster-whisper model (default: small)
    PYTB_WHISPER_MODEL_DIR      download/cache directory for that model
    PYTB_FASTER_WHISPER_PATH    extra site-packages dir for faster-whisper
                                 (appended last, so its bundled onnxruntime
                                 cannot shadow the TTS environment's one)
"""

import io
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# A --target install of faster-whisper also ships onnxruntime, which may be
# binary-incompatible with VieNeu's ONNX models. Put that directory at the
# *end* of sys.path so faster-whisper can be imported without replacing the
# TTS environment's onnxruntime.
_stt_path = os.environ.get("PYTB_FASTER_WHISPER_PATH")
if _stt_path:
    _stt_path = os.path.abspath(_stt_path)
    while _stt_path in sys.path:
        sys.path.remove(_stt_path)
    sys.path.append(_stt_path)

from webui import reader


@unittest.skipUnless(
    os.environ.get("PYTB_TTS_STT") == "1",
    "set PYTB_TTS_STT=1 to run the real TTS→STT regression test",
)
class TestTtsSttShortClause(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from vieneu import Vieneu
        except ImportError as e:
            raise unittest.SkipTest(f"vieneu is not installed: {e}")

        try:
            from faster_whisper import WhisperModel
        except ImportError as e:
            raise unittest.SkipTest(f"faster-whisper is not installed: {e}")

        cls.eng = reader.TTSEngine()
        tts = Vieneu()
        cls.eng._tts = tts
        cls.eng.state = "ready"
        cls.eng.default_voice = tts._default_voice
        cls.eng.voices = [
            {"id": vid, "label": vid}
            for _desc, vid in tts.list_preset_voices()
        ]
        cls.sample_rate = int(getattr(tts, "sample_rate", 48000))
        cls.voice = os.environ.get("PYTB_TTS_VOICE") or "Ngọc Huyền"
        if cls.voice not in {v["id"] for v in cls.eng.voices}:
            raise unittest.SkipTest(f"voice {cls.voice!r} is not available")

        kwargs = {"device": "cpu", "compute_type": "int8"}
        model_dir = os.environ.get("PYTB_WHISPER_MODEL_DIR")
        if model_dir:
            kwargs["download_root"] = model_dir
        cls.model_name = os.environ.get("PYTB_WHISPER_MODEL") or "small"
        cls.stt = WhisperModel(cls.model_name, **kwargs)

    def _transcribe(self, wav_bytes):
        import librosa
        import soundfile as sf

        wav, sr = sf.read(io.BytesIO(wav_bytes), dtype="float32")
        if sr != 16000:
            wav = librosa.resample(wav, orig_sr=sr, target_sr=16000)
        segments, _info = self.stt.transcribe(
            wav,
            language="vi",
            beam_size=5,
            vad_filter=False,
            condition_on_previous_text=False,
        )
        return " ".join(s.text.strip() for s in segments)

    @staticmethod
    def _count_words(text, phrase):
        """Count *phrase* as whole words, ignoring punctuation and case."""
        text = re.sub(r"[^\w\s]", " ", text.casefold(), flags=re.UNICODE)
        text = re.sub(r"\s+", " ", text).strip()
        return f" {text} ".count(f" {phrase.casefold()} ")

    def test_ch152_sentence_is_spoken_once(self):
        # The reader splits the paragraph at the comma. Both pieces used to
        # come back with a free-running second copy.
        text = "“Đi thôi, đi tắm thôi.”"
        wav, _segs = self.eng.synthesize(text, voice=self.voice)
        said = self._transcribe(wav)
        self.assertLessEqual(
            self._count_words(said, "đi thôi"), 1,
            f"duplicated 'đi thôi': {said!r}",
        )
        self.assertLessEqual(
            self._count_words(said, "đi tắm thôi"), 1,
            f"duplicated 'đi tắm thôi': {said!r}",
        )
