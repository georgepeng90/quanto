"""Run real PPO training and frozen weekly/monthly ETF evaluations locally.

Defaults to quanto's existing synthetic sample generator. An explicit daily
dataset and optional training JSON can exercise already registered research data.
No market download, real order, or profitability assertion is made.
"""
from __future__ import annotations

import argparse
import io
import json
import math
import os
from pathlib import Path
import tempfile
import time
import zipfile

import pandas as pd

from scripts.verify_learning_system import ResourceSampler


def default_spec(bundle, frequency, timesteps):
    dates = pd.DatetimeIndex(bundle.calendar.date)
    dates = dates[(dates >= pd.Timestamp(bundle.manifest.data_start)) & (dates <= pd.Timestamp(bundle.manifest.data_end))]
    start, first, second = 120, int(len(dates) * .6), int(len(dates) * .8)
    if first - start < 252 or len(dates) - second < 40:
        raise ValueError('default experiment needs at least four years; supply --training-json for shorter fixtures')
    day = lambda index: dates[index].date().isoformat()
    return dict(name=f'{frequency} acceptance PPO', frequency=frequency, lookback=120,
        backtest=dict(start=day(start), end=day(-1), symbols=list(bundle.instruments.symbol.astype(str)),
            initial_cash=1000000, seed=42,
            execution=dict(commission_rate=.0003, minimum_commission=5, slippage_rate=.0005,
                           max_stale_sessions=5, model='next_open'),
            risk=dict(max_total_weight=.95, max_symbol_weight=.35, group_caps={}, stress_scenarios=[],
                      pause_action='block_new', recovery_mode='never'),
            metrics=dict(annual_sessions=252, risk_free_rate=0, cash_rate=0, definition_version='v1')),
        train_start=day(start), train_end=day(first - 1), validation_start=day(first), validation_end=day(second - 1),
        test_start=day(second), test_end=day(-1), episode_sessions=252,
        total_timesteps=timesteps, n_steps=32, batch_size=16, n_epochs=2, hidden_size=32,
        eval_every_timesteps=64, seed=42, device='cpu')


def run(root, args, report):
    from fastapi.testclient import TestClient
    from quanto.api.app import create_app
    from quanto.backtest.dataset_loaders import load_dataset
    from quanto.investing.service import InvestingService
    from quanto.learning.registry import hash_files, runtime_versions, verify_ref
    from quanto.sample_data import generate_sample_data

    source = args.dataset_dir
    if source is None:
        source = root / 'synthetic'
        generate_sample_data(source, years=4, seed=42)
    bundle = load_dataset(source)
    training = json.loads(args.training_json.read_text(encoding='utf-8')) if args.training_json else default_spec(bundle, args.frequency, args.timesteps)
    app = create_app(root / 'workspace')
    dataset = app.state.service.store.register_dataset(source)
    report.update(source=bundle.manifest.model_dump(mode='json'), runtime=runtime_versions(), dataset_id=dataset.id,
                  dataset_content_hash=dataset.content_hash, training=training)
    (root / 'train-request.json').write_text(json.dumps({'dataset_id': dataset.id, 'spec': training}, ensure_ascii=False, indent=2), encoding='utf-8')
    with TestClient(app, base_url='http://127.0.0.1') as client:
        headers = {'Authorization': f'Bearer {client.get("/api/session").json()["token"]}'}

        def request(method, path, **kwargs):
            response = client.request(method, path, headers=headers, **kwargs)
            if not response.is_success:
                raise RuntimeError(f'{response.status_code}: {response.text}')
            return response

        def submit(stage, route, body):
            begin = time.perf_counter()
            job = request('POST', f'/api/investing/{route}', json=body).json()
            report['stages'][stage] = {'job_id': job['id']}
            print(f'{stage}: {job["id"]}', flush=True)
            deadline = time.monotonic() + args.timeout
            while job['state'] in {'queued', 'running', 'cancel_requested'}:
                if time.monotonic() > deadline:
                    request('POST', f'/api/jobs/{job["id"]}/cancel', json={})
                    raise TimeoutError(f'{stage} timed out')
                time.sleep(.15)
                job = request('GET', f'/api/jobs/{job["id"]}').json()
            report['stages'][stage].update(seconds=time.perf_counter() - begin, state=job['state'])
            if job['state'] != 'succeeded':
                record = app.state.service.store.get_job(job['id'])
                trace = record.artifact_root / record.id / 'control' / 'traceback.txt'
                raise RuntimeError(f'{job}\n{trace.read_text(encoding="utf-8") if trace.exists() else ""}')
            return job['id']

        policy = submit('train', 'train', {'dataset_id': dataset.id, 'spec': training})
        service = InvestingService(app.state.service)
        policy_root = verify_ref(service.reference(policy, 'policy'))
        frozen = hash_files(policy_root)
        model = request('GET', f'/api/investing/policies/{policy}').json()['manifest']
        report['policy'] = model
        for baseline in ('policy', 'cash', 'equal_weight', 'buy_hold'):
            identity = submit(baseline, 'backtests', {'dataset_id': dataset.id, 'policy_id': policy,
                'spec': dict(start=training['test_start'], end=training['test_end'], baseline=baseline, cost_multiplier=1)})
            summary = request('GET', f'/api/investing/backtests/{identity}').json()['manifest']
            root_result = verify_ref(service.reference(identity, 'policy_backtest'))
            equity = pd.read_csv(root_result / 'equity.csv')
            initial = training['backtest']['initial_cash']
            assert math.isclose(math.log(equity.equity.iloc[-1] / initial), summary['metrics']['net_log_return'], abs_tol=1e-10)
            chart = request('GET', f'/api/investing/backtests/{identity}/charts?max_points=50').json()
            assert chart['total_count'] == len(equity) and len(chart['points']) <= 50
            rows = request('GET', f'/api/investing/backtests/{identity}/tables/positions?limit=5').json()
            assert rows['total_count'] == len(equity) * len(training['backtest']['symbols'])
            exported = request('GET', f'/api/investing/backtests/{identity}/export').content
            with zipfile.ZipFile(io.BytesIO(exported)) as archive:
                assert archive.testzip() is None and 'equity.csv' in archive.namelist()
            assert frozen == hash_files(policy_root)
            report['stages'][baseline].update(metrics=summary['metrics'], counts=summary['counts'], export_bytes=len(exported))
        report['checks'] = ['supervised PPO train/save/load', 'validation selection before test', 'four frozen baselines',
                            'reward/net-equity reconciliation', 'daily chart/table/export', 'unchanged model hashes']


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('outputs/investing-acceptance'))
    parser.add_argument('--dataset-dir', type=Path)
    parser.add_argument('--training-json', type=Path)
    parser.add_argument('--frequency', choices=('weekly', 'monthly'), default='weekly')
    parser.add_argument('--timesteps', type=int, default=128)
    parser.add_argument('--timeout', type=int, default=600)
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix=f'{args.frequency}-', dir=args.output)).resolve()
    config = root / 'market-config.json'
    config.write_text(json.dumps({'root': str(root / 'market')}), encoding='utf-8')
    previous = os.environ.get('QUANTO_MARKET_CONFIG')
    os.environ['QUANTO_MARKET_CONFIG'] = str(config)
    report = dict(state='running', root=str(root), stages={})
    sampler = ResourceSampler(root)
    begin = time.perf_counter()
    sampler.thread.start()
    try:
        run(root, args, report)
        report['state'] = 'passed'
    except BaseException as exc:
        report.update(state='failed', error=str(exc))
        raise
    finally:
        sampler.stop()
        report.update(seconds=time.perf_counter() - begin, peak_rss_bytes=sampler.peak_rss, peak_disk_bytes=sampler.peak_disk)
        (root / 'acceptance.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        if previous is None:
            os.environ.pop('QUANTO_MARKET_CONFIG', None)
        else:
            os.environ['QUANTO_MARKET_CONFIG'] = previous
        print(f'{report["state"]}: {root / "acceptance.json"}', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
