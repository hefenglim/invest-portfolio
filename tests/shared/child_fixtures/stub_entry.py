"""``stub_entry <out_json>``: a worker ``main()`` in miniature — apply the test stubs from the
environment first, then report what the code it binds would see."""

from __future__ import annotations

import sys

from portfolio_dash.shared.child_runner import apply_test_stubs_from_env
from tests.shared.child_fixtures import stub_target
from tests.shared.child_fixtures._io import write_json


def main() -> None:
    applied = apply_test_stubs_from_env()      # raises (exit 1) without PD_TEST_MODE=1
    write_json(sys.argv[1], {"applied": applied, "value": stub_target.VALUE})


if __name__ == "__main__":
    main()
