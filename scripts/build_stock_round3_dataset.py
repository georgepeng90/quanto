"""Freeze this finite public-source stock experiment; fail on unverifiable execution rows."""
import base64
from collections import Counter
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
import gzip
import hashlib
import json
from pathlib import Path
import zipfile

import pandas as pd

from quanto.backtest.dataset_identity import make_import_evidence, standard_table_semantics, validate_binding_tables
from quanto.backtest.dataset_loaders import load_dataset
from quanto.backtest.stock_data import validate_stock_data, STOCK_DIVIDEND_LIMITATION
from quanto.data.contracts import InstrumentId
from quanto.framework import AssetBinding, AssetMapping, BenchmarkBinding, ResearchInterval, TableSemanticBinding

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / 'outputs/stock-round3-training-20261014'
PUBLIC = BASE / 'public-history-v2'
HERE = PUBLIC / 'integration'
RAW = ROOT / '.superpowers/screening-round2-20261014/raw'
OUT = BASE / 'dataset'
START, END, EXEC_START = '2024-01-02', '2026-10-13', '2024-08-05'
SHA = lambda data: hashlib.sha256(data).hexdigest()
READ = lambda path: json.loads(path.read_text(encoding='utf-8-sig'))
CENTS = lambda value: Decimal(str(value)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
OUT.mkdir(exist_ok=True)
sources, source_keys = [], set()


def source(ref, payload, url, retrieved, *, available=None, published=None, coverage=(START, END), provenance=None):
    assert ref not in source_keys and payload
    source_keys.add(ref)
    # Unknown publication times are explicitly retrieval upper bounds, for retrospective ledger sources only.
    available = available or retrieved
    published = published or available
    sources.append(dict(source_ref=ref, source_url=url, payload_base64=base64.b64encode(payload).decode(),
        payload_sha256=SHA(payload), published_at=published, available_at=available, retrieved_at=retrieved,
        coverage_start=coverage[0], coverage_end=coverage[1], provenance=json.dumps(provenance or {
            'time_basis': 'retrospective retrieval upper bound; original publication time unknown'
        }, ensure_ascii=False, separators=(',', ':'))))
    return ref


def archived(ref, path, meta=None, **kwargs):
    meta = meta or READ(Path(str(path) + '.meta.json'))
    raw = path.read_bytes()
    assert SHA(raw) == meta['sha256'].lower() and len(raw) == meta['bytes'], path
    assert meta.get('status', meta.get('status_code', 200)) == 200
    return source(ref, raw, meta.get('url', meta.get('final_url')), meta.get('retrieved_at', meta.get('retrieved_at_utc')), **kwargs)


basket = READ(BASE / 'basket.json')
members = basket['members']
assert len(members) == 34
symbols = {m['code']: f"{m['exchange']}:stock:{m['code']}" for m in members}
profiles = READ(ROOT / '.superpowers/screening-round2-20261014/details.json')
bars = {code: {r['date']: r for r in READ(HERE / (code + '.json'))} for code in symbols}
cross = READ(HERE / 'quote-crosscheck.json')['counts']
assert all(cross.get(k + '_different', 0) == 0 and cross[k + '_equal'] == 22771 for k in ('open', 'high', 'low', 'close'))

# All dated official rules, fee statutes and annual closure notices are frozen with the dataset.
rule_refs = []
for item in READ(PUBLIC / 'fees-calendar/rules/source-index.json')['files']:
    rule_refs.append(archived('rule:' + item['file'], PUBLIC / 'fees-calendar/rules' / item['file'], item,
        available=item['publication_date'] + 'T23:59:59+08:00',
        provenance={'publication_precision': 'date', 'time_basis': 'conservative end-of-publication-day bound, not observed publication second',
                    'supports': item['supports']}))
fee_refs = {}
for item in READ(PUBLIC / 'fees-calendar/source-index.json')['sources']:
    if item['published_date'] is None:
        continue  # The missing original ChinaClear page is diagnostic, never evidence.
    fee_refs[item['file']] = archived('fee-calendar:' + item['file'], PUBLIC / 'fees-calendar' / item['file'], item,
        available=item['published_date'] + 'T23:59:59+08:00',
        provenance={'publication_precision': 'date', 'time_basis': 'conservative end-of-publication-day bound',
                    'primary': item['is_primary_source'], 'supports': item['supports']})
calendar = pd.read_csv(PUBLIC / 'fees-calendar/cn-a-share-trading-calendar-2024-01-02_to_2026-10-13.csv')
days = calendar.loc[calendar.exchange_status.eq('open'), 'date'].tolist()
assert len(days) == 670 and all(set(series) == set(days) for series in bars.values())
pd.DataFrame({'date': days + ['2026-10-14']}).to_parquet(OUT / 'calendar.parquet', index=False)

instruments, prices = [], []
for code, symbol in symbols.items():
    profile = profiles[code]['profile']
    basic, issuance = profile['jbzl'][0], profile['fxxg'][0]
    assert basic['SECURITY_CODE'] == code and basic['SECURITY_TYPE'] in ('上交所主板A股', '深交所主板A股')
    listing = issuance['LISTING_DATE'][:10]
    assert listing <= '2021-12-31'
    instruments.append(dict(symbol=symbol, lot_size=100, settlement_sessions=1, listing_date=listing,
                            group=basic['EM2016'].split('-')[0], board='main_board'))
    for kind in ('profile', 'bars'):
        archived(f'eastmoney:{code}:{kind}', RAW / f'{code}-{kind}.json', READ(RAW / f'{code}-{kind}.meta.json'))
    meta = READ(HERE / (code + '.meta.json'))
    payload = (HERE / (code + '.response.bin')).read_bytes()
    assert SHA(payload) == meta['response.bin']['sha256']
    source('baostock:' + code, payload, meta['endpoint'], meta['retrieved_at'],
           provenance={'adjustflag': 3, 'units': 'volume=shares; amount=CNY', 'request_sha256': meta['request.bin']['sha256']})
    for day, row in bars[code].items():
        assert row['isST'] == '0'
        if row['tradestatus'] == '1':
            prices.append(dict(date=day, symbol=symbol, **{key: float(row[key]) for key in ('open','high','low','close','volume','amount')}))
        else:
            assert code == '002128' and '2025-05-06' <= day <= '2025-05-16' and not row['volume']
pd.DataFrame(instruments).to_parquet(OUT / 'instruments.parquet', index=False)
pd.DataFrame(prices).to_parquet(OUT / 'prices.parquet', index=False)
assert len(prices) == 22771
benchmark_raw = READ(RAW / 'csi300-bars.json')['data']
assert benchmark_raw['code'] == '000300'
benchmark = [dict(date=x[0], close=float(x[2]), turnover=float(x[6])) for x in (v.split(',') for v in benchmark_raw['klines'])]
assert {r['date'] for r in benchmark} == set(days)
archived('benchmark:csi300', RAW / 'csi300-bars.json', READ(RAW / 'csi300-bars.meta.json'))
pd.DataFrame(benchmark).to_parquet(OUT / 'benchmark.parquet', index=False)

# Complete finite announcement corpus, including PDFs used to exclude unsupported holder events.
action_refs = []
for meta_path in sorted((PUBLIC / 'actions').rglob('*.meta.json')):
    path = Path(str(meta_path).removesuffix('.meta.json'))
    if not path.is_file():
        continue
    action_refs.append(archived('action-corpus:' + path.relative_to(PUBLIC / 'actions').as_posix(), path, READ(meta_path)))
verification = READ(PUBLIC / 'actions/verification.json')
assert verification['all_announcement_titles_scanned'] == 11898 and verification['holder_action_title_matches_reviewed'] == 225
assert verification['engine_dividend_events'] == 148 and verification['engine_split_events'] == 10
assert verification['cash_amounts_crosschecked_against_pdf'] == 148
assert len(action_refs) == sum(verification['source_file_counts'].values()) == 601
for name, digest in verification['artifacts'].items():
    assert SHA((PUBLIC / 'actions' / name).read_bytes()) == digest, name
actions = READ(PUBLIC / 'actions/action-ledger.json')['actions']
assert len(actions) == 148
for action in actions:
    path = BASE / action['pdf_path']
    assert SHA(path.read_bytes()) == action['pdf_sha256']
    assert action['pay_date'] == action['ex_date']
    if action['split_ratio'] is not None:
        assert action['bonus_listing_date'] == action['ex_date']
events = READ(PUBLIC / 'actions/engine-events.json')
assert len(events) == 158
pd.DataFrame(events).to_parquet(OUT / 'events.parquet', index=False)
coverage_ref = archived('corporate-action-coverage', HERE / 'rules/basket-announcements.json',
    provenance={'interpretation': 'This response is only the corpus index anchor. Complete coverage depends on all linked archived pages, implementation PDFs and title dispositions.',
                'supporting_source_refs': action_refs, 'finite_coverage': verification['coverage_limit'],
                'derived_artifact_hashes': verification['artifacts']})

# Halt start is independently published before the first halted opening; the end is not foreknown.
halt_dir = HERE / 'halt'
halt_ref = archived('halt:002128:2025-05-06', halt_dir / 'sina-2025-05-06-0730.html',
    available='2025-05-06T07:30:10+08:00', published='2025-05-06T07:30:02+08:00',
    coverage=('2025-05-06', '2025-05-16'),
    provenance={'time_basis': 'publisher explicit +08:00 metadata corroborated by May5 China Fund News and official SZSE notice',
                'as_of_fact': 'halt from May6 until further resumption notice; eventual final halt date not known at start',
                'realized_interval': 'corroborated by daily halted status, empty TDX histories and May19 official resumption'})
for item in READ(halt_dir / 'evidence.json')['evidence']:
    if item['file'] != 'sina-2025-05-06-0730.html':
        archived('halt-support:' + item['file'], halt_dir / item['file'])

# Opening access is a conservative execution gate, not a guarantee of fill or quote-queue priority.
opening, daily_status, counts = [], [], Counter()
unknown_warmup = []
for batch in [json.loads(x) for x in (PUBLIC / 'opening/tdx-opening-matches.ndjson').read_text(encoding='utf-8').splitlines()]:
    day = batch['date']
    pages = json.loads(gzip.decompress((PUBLIC / 'opening/tdx-raw-pages' / (day + '.json.gz')).read_bytes()))['pages']
    for tdx_code, match in batch['matches'].items():
        code, symbol = tdx_code[2:], symbols[tdx_code[2:]]
        bar = bars[code][day]
        status, buy, sell, available, ref = 'trading', False, False, None, None
        if bar['tradestatus'] == '0':
            assert match is None
            status, available, ref = 'suspended', '2025-05-06T07:30:10+08:00', halt_ref
        else:
            if match is not None:
                page = pages[tdx_code]
                assert match['time_label'] == '09:25' and match['status_raw'] != 8
            else:
                gap = json.loads(gzip.decompress((PUBLIC / 'opening/tdx-gap-pages' / f'{day}-{tdx_code}.json.gz').read_bytes()))
                match = gap['oldest_record']
                page = next((p for p in gap['pages'] if match and match['record_hex'] in p['raw_payload_hex']), None)
            if match is not None and match['time_label'] < '09:30' and CENTS(match['price']) == CENTS(bar['open']):
                raw = bytes.fromhex(page['raw_payload_hex'])
                assert SHA(raw) == page['raw_payload_sha256'] and bytes.fromhex(match['record_hex']) in raw
                reference = CENTS(page['price_base_raw_f32'])
                assert reference == CENTS(bar['preclose'])
                upper, lower = CENTS(reference * Decimal('1.1')), CENTS(reference * Decimal('.9'))
                price = CENTS(match['price'])
                assert lower <= price <= upper
                buy, sell = price < upper, price > lower
                # Packet timestamps are minute buckets. Use the latest possible second of that minute.
                available = day + 'T' + match['time_label'] + ':59+08:00'
                if day >= EXEC_START:
                    ref = source(f'opening:{tdx_code}:{day}', raw, 'tcp://116.205.183.150:7709', page['retrieved_at'],
                        available=available, published=day + 'T' + match['time_label'] + ':00+08:00', coverage=(day, day),
                        provenance={'time_basis': 'selected real market-event minute interval, not historical query or vendor archive publication time',
                            'event_minute': match['time_label'], 'timestamp_precision': 'minute',
                            'selected_record_hex': match['record_hex'], 'reference_price': str(reference),
                            'opening_price': str(price), 'upper_limit': str(upper), 'lower_limit': str(lower),
                            'query': page.get('query') or {'method':'0x0fc6','code':tdx_code,'date':day,'start':page['start']},
                            'rule': 'opening-print-limit-gate-v1',
                            'market_publication_basis': 'official exchange realtime-market dissemination rule; source queried retrospectively',
                            'supporting_source_refs': [r for r in rule_refs if r.startswith('rule:' + ('sse' if tdx_code.startswith('sh') else 'szse') + ('-2023' if day < '2026-07-06' else '-2026'))],
                            'raw_page_scope': 'Contains other intraday records; only selected opening record and reference are execution inputs.'})
                    counts[match['time_label']] += 1
            else:
                unknown_warmup.append([day, symbol, match['time_label'] if match else None])
                assert day < EXEC_START, (day, code, 'not proven by 09:30:00')
        daily_status.append(dict(date=day, symbol=symbol, status=status, can_buy=buy, can_sell=sell))
        if day >= EXEC_START:
            assert available and ref
            opening.append(dict(date=day, symbol=symbol, status=status, can_buy=buy, can_sell=sell,
                                available_at=available, source_ref=ref))
            counts[status] += 1
            if status == 'trading':
                counts['blocked_buy'] += not buy
                counts['blocked_sell'] += not sell
assert len(opening) == sum(d >= EXEC_START for d in days) * 34
pd.DataFrame(daily_status).to_parquet(OUT / 'status.parquet', index=False)
pd.DataFrame(opening).to_parquet(OUT / 'stock_open_status.parquet', index=False)
tax_ref = fee_refs['stamp-duty-half-announcement-2023.html']
transfer_ref = fee_refs['chinaclear-transfer-notice-sina-secondary.html']
fees = [dict(exchange=exchange, instrument_type='stock', board='main_board', side=side,
    effective_from='2024-01-02', effective_to='2026-10-14', stamp_duty_rate=.0005 if side == 'sell' else 0.,
    transfer_fee_rate=.00001, available_at='2023-08-28T23:59:59+08:00', source_ref=tax_ref,
    stamp_source_ref=tax_ref, transfer_source_ref=transfer_ref) for exchange in ('SSE','SZSE') for side in ('buy','sell')]
pd.DataFrame(fees).to_parquet(OUT / 'fee_rules.parquet', index=False)
pd.DataFrame(sources).to_parquet(OUT / 'stock_sources.parquet', index=False)

limitations = [STOCK_DIVIDEND_LIMITATION,
    'Contemporary October2026 stock selection and current industry labels followed by historical training are development research, not historical stock-selection validation.',
    'Opening permissions are conservative rule-derived gates from real pre-09:30 trade prints, public halt notices and mainboard limit rules. They do not establish queue priority, spread, depth or guaranteed fills.',
    'TDX event times have minute precision. The selected event is bounded by the end of its minute; actual retrieval is October2026. No claim of vendor archive publication at event time is made.',
    'Training/execution starts 2024-08-05. Earlier data are price-feature warmup only. Five earlier missing or 09:30-only opening records have both warmup flags conservatively false; they are not certified historical permissions.',
    'The initial July8 candidate start was rejected solely for a July31 opening record lacking second precision; final start is the next Monday after that evidence gap.',
    'Corporate-action coverage is a finite audit of 11898 archived public announcement titles and supporting notices, subject to omissions in the public catalog.',
    'Transfer fee history uses a contemporaneous secondary reproduction attributed to ChinaClear; rates are .00001 bilateral and stamp duty .0005 sell-only. Broker commission .0003 minimum5 and slippage .0005 are protocol assumptions.',
    'CSI300 turnover is the index constituent aggregate series from Eastmoney, not total exchange turnover.',
    'Calendar includes one following session 2026-10-14 derived from weekday and the archived 2026 official annual closure schedules.'
]
manifest = dict(source='BaoStock unadjusted OHLCV independently checked against Eastmoney; original public TDX records; archived company notices and exchange rules',
    is_demo=False, price_mode='raw_cash', currency='CNY', calendar_start=START, calendar_end='2026-10-14',
    data_start=START, data_end=END, volume_unit='share', amount_unit='CNY', event_coverage='complete', benchmark_type='price_index',
    field_semantics={'stock_event_coverage_source':coverage_ref, 'stock_open_status':'opening-print-limit-gate-v1; execution event only',
        'prices':'raw unadjusted, volume shares and amount CNY; no fake bars for suspended dates',
        'events':'gross actual entitled-share dividend cash and bonus split ratios, implementation-PDF confirmed dates',
        'source_availability':'Historical selected event availability differs from actual retrieval. Unknown document timestamps use retrieval upper bounds; date-only official rules use explicit end-of-day bounds.'},
    universe_rule={'min_history_days':120,'min_avg_turnover_20d':0},
    auxiliary_tables={name:name+'.parquet' for name in ('fee_rules','stock_open_status','stock_sources')}, limitations=limitations)
(OUT / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
bundle = load_dataset(OUT)
evidence_ref = 'public-stock-round3-20261014'
semantics = list(standard_table_semantics(bundle, evidence_ref))
for name, semantic_id, keys in (('fee_rules','stock-fee-rules',('exchange','board','side','effective_from')),
    ('stock_open_status','stock-open-status',('symbol','date')), ('stock_sources','stock-sources',('source_ref',))):
    frame = bundle.auxiliary[name]
    schema = {col:{'dtype': 'datetime' if col in ('available_at','published_at','retrieved_at') else 'date' if col in ('date','coverage_start','coverage_end','effective_from','effective_to') else 'bool' if pd.api.types.is_bool_dtype(frame[col]) else 'float' if pd.api.types.is_numeric_dtype(frame[col]) else 'string',
                   'unit': 'flag' if col in ('can_buy','can_sell') else 'rate' if col.endswith('_rate') else 'reference'} for col in frame}
    semantics.append(TableSemanticBinding(table=name, semantic_id=semantic_id, version='1', schema_definition=schema,
        temporal_rules={'availability_rule':'execution_event','business_keys':keys,'assumption':'Execution or lineage evidence only, never observation features.'}, evidence_ref=evidence_ref))
assets = tuple(AssetMapping(local_symbol=symbols[m['code']], instrument_id=InstrumentId(**m), evidence_ref=evidence_ref) for m in members)
binding = AssetBinding(dataset_version_id='pending-stock-import', dataset_content_hash='0'*64, assets=assets,
    benchmark=BenchmarkBinding(instrument_id=InstrumentId(exchange='SSE',instrument_type='index',code='000300'),
                               return_basis='price_index',evidence_ref=evidence_ref), table_semantics=tuple(semantics))
validate_binding_tables(bundle, binding.assets, binding.table_semantics)
context = validate_stock_data(bundle, binding, tuple(symbols.values()), ResearchInterval(start=date.fromisoformat(EXEC_START),end=date.fromisoformat(END),warmup_start=date.fromisoformat(START)))
evidence = make_import_evidence(OUT, assets=assets, benchmark=binding.benchmark, source_document='Archived public sources and finite action audit for frozen third-round 34-stock basket',table_semantics=binding.table_semantics)
(OUT / 'identity_evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2),encoding='utf-8')
summary = dict(status='local_validation_passed', execution_start=EXEC_START, data_start=START, data_end=END,
    execution_sessions=sum(d >= EXEC_START for d in days), warmup_sessions=sum(d < EXEC_START for d in days),
    prices=len(prices), stocks=34, events=len(events), opening_rows=len(opening), source_rows=len(sources),
    opening_counts=dict(counts), unknown_warmup=unknown_warmup, fee_keys=len(context.fees),
    files=evidence['input_files'], coverage_audit=verification['coverage_limit'],limitations=limitations)
(HERE / 'dataset-validation.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
with zipfile.ZipFile(BASE / 'dataset.zip','w',compression=zipfile.ZIP_DEFLATED) as z:
    for path in sorted(OUT.iterdir()):
        z.write(path,path.name)
print(json.dumps({k:v for k,v in summary.items() if k not in ('files','coverage_audit','limitations')}, ensure_ascii=False))
print('zip_bytes', (BASE / 'dataset.zip').stat().st_size, flush=True)
