"""Published DDS regression table used for a real doctor solve, not scoring.

Source: official DDS 2.9.0 hands/list100.txt, fourth deal,
https://github.com/dds-bridge/dds/blob/8d75755c8df81999557758c9757514edb94017bc/hands/list100.txt
Its unequal E/W results detect declarer/leader and seat-orientation mistakes.
"""

from bridge_evolve.oracle.dds import DDSAdapter, DDSConfigurationError
from bridge_evolve.oracle.pbn import deal_from_pbn
from bridge_evolve.oracle.table import DDTable

VERIFICATION_PBN = (
    "W:AK8.A83.KT.KQJ98 QJT96543.4.AJ.A5 .KQJ976.742.7642 72.T52.Q98653.T3"
)
VERIFICATION_TABLE = DDTable(
    ((2, 7, 2, 8, 2), (10, 6, 10, 4, 10), (2, 7, 2, 8, 2), (11, 6, 11, 5, 11))
)


def verify_adapter(adapter: DDSAdapter) -> None:
    actual = adapter.solve(deal_from_pbn(VERIFICATION_PBN))
    if actual != VERIFICATION_TABLE:
        raise DDSConfigurationError(
            "DDS real 20-entry published-fixture self-test failed; "
            f"expected={VERIFICATION_TABLE.to_dict()}; actual={actual.to_dict()}"
        )
