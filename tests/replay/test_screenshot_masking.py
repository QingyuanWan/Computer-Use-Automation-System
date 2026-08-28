"""§3.4 declaration-driven screenshot masking: sensitive-bound input regions are masked in evidence shots."""
from pathlib import Path

from src.executor.evidence import EvidenceCapture
from src.replay.engine import sensitive_bound_locators
from src.storage import ArtifactStorage

_ROOT = Path(__file__).resolve().parent.parent.parent


def test_sensitive_bound_locators_from_bundled_lookup():
    # lookup_checking_balance declares username + password sensitive: true, each typed by a type_text step,
    # so exactly those two input locators must be masked. account_id is NOT sensitive -> not masked.
    art = ArtifactStorage(_ROOT / "artifacts").load("lookup_checking_balance")
    locs = sensitive_bound_locators(art)
    assert len(locs) == 2


class _FakePage:
    def __init__(self):
        self.kwargs = None

    async def screenshot(self, **kwargs):
        self.kwargs = kwargs


async def test_capture_passes_mask_when_registered(tmp_path):
    page = _FakePage()
    ev = EvidenceCapture(page, tmp_path)
    ev.mask_locators = ["LOC_A", "LOC_B"]          # stand-ins for Playwright Locators
    await ev.capture("checkpoint_timeout")
    assert page.kwargs["mask"] == ["LOC_A", "LOC_B"]
    assert page.kwargs["mask_color"] == "#000000"


async def test_capture_omits_mask_when_none(tmp_path):
    page = _FakePage()
    ev = EvidenceCapture(page, tmp_path)
    await ev.capture("checkpoint_timeout")
    assert "mask" not in page.kwargs
