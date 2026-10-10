import { render, screen } from '@testing-library/react'
import { expect, test } from 'vitest'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { FrameworkProvenance } from '../src/components/FrameworkProvenance'
import { BacktestCatalog } from '../src/components/BacktestCatalog'
import type { BacktestCatalogRow } from '../src/types'

const framework = {
  basket: { basket_id: 'selected', revision: 2, selection_note: '人工选择两只公司' },
  ordered_pairs: [
    { instrument_id: { exchange: 'SSE', instrument_type: 'stock', code: '000001' }, local_symbol: 'A' },
    { instrument_id: { exchange: 'SZSE', instrument_type: 'stock', code: '000001' }, local_symbol: 'B' },
  ],
  asset_binding: { benchmark: { instrument_id: { exchange: 'SSE', instrument_type: 'index', code: '000300' }, return_basis: 'price_index' } },
  request: { spec: { evaluation_purpose: 'development_replay' } },
  request_hash: 'frozen-request', definitions: [],
}

test('shows canonical identities in model order, selection context and explicit evaluation purpose', async () => {
  render(<FrameworkProvenance value={{ framework }} />)
  await userEvent.click(screen.getByText(/证券身份与输入顺序/))
  expect(screen.getByText('SSE:stock:000001 → A')).toBeVisible()
  expect(screen.getByText('SZSE:stock:000001 → B')).toBeVisible()
  expect(screen.getByText(/人工选择两只公司/)).toBeVisible()
  expect(screen.getByText(/开发回放/)).toBeVisible()
  expect(screen.getByText(/价格指数/)).toBeVisible()
  expect(screen.queryByText('盲测')).toBeNull()
})

test('legacy reports do not gain proven identity from bare symbols', () => {
  render(<FrameworkProvenance value={{ symbols: ['000001'], sample_purpose: '旧研究' }} />)
  expect(screen.getByText(/证券身份未知/)).toBeVisible()
  expect(screen.queryByText('SSE:stock:000001')).toBeNull()
})

test('best weights and last checkpoint retain distinct sources and steps', () => {
  render(<FrameworkProvenance value={{ framework, execution_provenance: {
    best_checkpoint: { source_content_id: 'ancestor-1', source: 'ancestor', timesteps: 128 },
    last_checkpoint: { source_content_id: 'current-2', source: 'current_artifact', cumulative_timesteps: 1024, session_timesteps: 256 },
    training_boundaries: { train_start: '2023-01-01', train_end: '2023-12-31', validation_start: '2024-01-01', validation_end: '2024-03-31', test_start: null, test_end: null },
  } }} />)
  expect(screen.getByText(/最佳权重.*ancestor-1.*128/)).toBeVisible()
  expect(screen.getByText(/末次检查点.*1024.*256/)).toBeVisible()
  expect(screen.getByText(/测试成绩未评估/)).toBeVisible()
})

const catalogRow = (id: string, changes: Partial<BacktestCatalogRow> = {}): BacktestCatalogRow => ({
  id, job_id: id, kind: 'fixed', name: id, method: 'fixed', state: 'succeeded', created_at: null,
  finished_at: null, error: null, start: '2024-01-01', end: '2024-12-31', symbols: ['000001'],
  metrics: { total_return: .1 }, conditions: {}, comparison_key: 'economic-key', detail_url: `/runs/${id}`,
  identity_status: 'proven', asset_types: ['stock'], canonical_assets: ['SZSE:stock:000001'],
  basket_revision_ref: { basket_id: 'companies', revision: 2 }, dataset_source: 'Source A', ...changes,
})

test('catalog filters proven canonical assets, basket revisions and saved data sources', async () => {
  render(<MemoryRouter><BacktestCatalog rows={[catalogRow('company'), catalogRow('fund', {
    asset_types: ['etf'], canonical_assets: ['SSE:etf:000001'], basket_revision_ref: { basket_id: 'funds', revision: 1 }, dataset_source: 'Source B',
  }), catalogRow('old', { identity_status: 'unknown', canonical_assets: [], comparison_key: null })]} complete busy={false} refresh={() => {}} /></MemoryRouter>)
  await userEvent.selectOptions(screen.getByLabelText('资产类型筛选'), 'stock')
  expect(screen.getByRole('link', { name: 'company' })).toBeVisible()
  expect(screen.queryByRole('link', { name: 'old' })).toBeNull()
  expect(screen.queryByRole('link', { name: 'fund' })).toBeNull()
  await userEvent.selectOptions(screen.getByLabelText('资产类型筛选'), 'all')
  await userEvent.selectOptions(screen.getByLabelText('篮子修订筛选'), 'funds@1')
  expect(screen.getByRole('link', { name: 'fund' })).toBeVisible()
  await userEvent.selectOptions(screen.getByLabelText('数据来源筛选'), 'Source A')
  expect(screen.queryByRole('link', { name: 'fund' })).toBeNull()
})

test('catalog trusts economic comparison key while disclosing method differences and excluding unknown identity', async () => {
  render(<MemoryRouter><BacktestCatalog rows={[catalogRow('fixed'), catalogRow('policy', { kind: 'policy', conditions: { learning_definition: 'PPO' } }), catalogRow('old', { identity_status: 'unknown' })]} complete busy={false} refresh={() => {}} /></MemoryRouter>)
  await userEvent.click(screen.getByLabelText('比较 fixed'))
  await userEvent.click(screen.getByLabelText('比较 policy'))
  expect(screen.getByText('条件一致，可直接比较这些结果。')).toBeVisible()
  expect(screen.getByText('条件差异')).toBeVisible()
  await userEvent.click(screen.getByLabelText('比较 old'))
  expect(screen.getByText(/不可直接排名/)).toBeVisible()
})
