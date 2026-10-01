from __future__ import annotations

import pandas as pd

from quanto.data.catalog import MarketCatalog
from quanto.data.contracts import DataRequest, FragmentRef, NormalizedChunk, SourceRecord
from quanto.data.storage import MarketStore
from quanto.data.validation import validate_chunk


def import_table(store: MarketStore, catalog: MarketCatalog, request: DataRequest, frame: pd.DataFrame,
                 source: SourceRecord, evidence: bytes) -> tuple[FragmentRef, ...]:
    fragments = store.put_chunk(validate_chunk(NormalizedChunk(request, frame, source, evidence)))
    catalog.register_fragments(fragments)
    return fragments
