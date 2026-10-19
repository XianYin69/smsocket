"""pytest root: make the package importable without installation.

Also auto-locates the SMS core client codec so the cross-repo drift guard
(test_dsm_drift) and integration suite (test_dsm_integration) run on this
machine without exporting SMS_CORE_DSM / SMS_CORE_SCRIPTS by hand: the dev
checkout next to this repo is the SMS-core folder.
"""
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _find_sms_core_scripts() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in [os.environ.get("SMS_CORE_DSM"), os.environ.get("SMS_CORE_SCRIPTS"),
                 os.path.join(os.path.dirname(here), "SMS-core", "skill", "scripts"),
                 os.path.join(os.path.dirname(here), "sms-core", "skill", "scripts")]:
        if cand and os.path.isfile(os.path.join(cand, "dsm.py")):
            return cand
    pat = os.path.join(os.path.dirname(here), "SMS-core*", "skill", "scripts", "dsm.py")
    hits = glob.glob(pat)
    return os.path.dirname(hits[0]) if hits else ""


_core = _find_sms_core_scripts()
if _core:
    if not os.environ.get("SMS_CORE_SCRIPTS"):
        os.environ["SMS_CORE_SCRIPTS"] = _core
    if not os.environ.get("SMS_CORE_DSM"):
        # The codec under test is sub_skills/dsm/dsm.py (the byte-identical
        # sibling of SMSocket/dsm.py).  skill/scripts/dsm.py is only a thin
        # re-export shim that would trick the drift guard into comparing the
        # shim against the server copy.
        real = os.path.join(os.path.dirname(os.path.dirname(_core)), "sub_skills", "dsm", "dsm.py")
        if not os.path.isfile(real):
            real = os.path.join(_core, "dsm.py")
        os.environ["SMS_CORE_DSM"] = real
