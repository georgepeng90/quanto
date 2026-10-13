import { test, expect } from '@playwright/test'
import { execFileSync } from 'node:child_process'
import { resolve } from 'node:path'

for (const assetType of ['etf', 'stock']) test(`${assetType}: creates an untrained model, continues it, and backtests the chosen saved version`, async ({ page }, testInfo) => {
  const root = resolve(import.meta.dirname, '../..')
  const python = resolve(root, process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python')
  const fixture = JSON.parse(execFileSync(python, ['-c', `
import json, sys
from pathlib import Path
from quanto.workbench.store import WorkbenchStore
from quanto.workbench.baskets import BasketStore
from quanto.workbench.bindings import BindingStore
from tests.fixtures.investing_training_cases import investing_case
from tests.fixtures.framework_imports import fixture_identity_evidence
from tests.fixtures.stock_training_cases import stock_case, stock_identity_evidence
workspace = Path(sys.argv[1])
is_stock = sys.argv[2] == 'stock'
assert workspace.resolve() != (Path.cwd() / 'workspace').resolve()
source = workspace / ('stock-e2e-data' if is_stock else 'model-e2e-data')
spec, _ = (stock_case if is_stock else investing_case)(source)
store = WorkbenchStore(workspace)
dataset = store.register_dataset(source, identity_evidence=(stock_identity_evidence if is_stock else fixture_identity_evidence)(source))
asset = BindingStore(store).get_asset_binding(dataset.id).assets[0].instrument_id
basket = BasketStore(store).create('Model E2E ' + sys.argv[2], (asset,), 'Synthetic training fixture', 'model-e2e-' + sys.argv[2])
print(json.dumps({'dataset_id': dataset.id, 'basket_id': basket.basket_id, 'spec': spec}))
`, process.env.QUANTO_E2E_WORKSPACE!, assetType], { cwd: root, encoding: 'utf8' }))
  const posts: { path: string; body: any }[] = []
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('request', request => { if (request.method() === 'POST' && request.url().includes('/api/investing/')) posts.push({ path: new URL(request.url()).pathname, body: request.postDataJSON() }) })
  const session = await (await page.request.get('/api/session')).json()
  const headers = { Authorization: `Bearer ${session.token}` }
  const definitions = (await (await page.request.get('/api/definitions', { headers })).json()).items
  const learning = definitions.find((item: any) => item.ref.kind === 'learning' &&
    item.supported_instrument_types.includes(assetType) &&
    item.action_version === 'hold-target-cash-quartiles-v2' && item.reward_version === 'net-log-return-v1')
  expect(learning).toBeTruthy()
  const tradingRef = learning.allowed_trading_refs[0]
  const trading = definitions.find((item: any) => item.ref.kind === tradingRef.kind &&
    item.ref.id === tradingRef.id && item.ref.version === tradingRef.version)
  expect(trading).toBeTruthy()
  const refKey = (ref: { kind: string; id: string; version: string }) => JSON.stringify([ref.kind, ref.id, ref.version])
  await page.goto('/models')
  await expect(page.getByRole('navigation', { name: '主导航' }).getByRole('link')).toHaveText(['数据', '固定策略', '强化学习模型', '回测'])
  await page.getByRole('button', { name: '新建模型', exact: true }).click()
  await page.getByRole('combobox', { name: '日线数据版本', exact: true }).selectOption(fixture.dataset_id)
  await page.getByRole('combobox', { name: '标的组合' }).selectOption(fixture.basket_id)
  await expect(page.getByRole('combobox', { name: '篮子修订' })).toHaveCount(0)
  await page.getByRole('combobox', { name: '学习定义' }).selectOption(refKey(learning.ref))
  await page.getByRole('combobox', { name: '交易定义' }).selectOption(refKey(trading.ref))
  await page.getByLabel('模型名称').fill('E2E 合成模型')
  await page.getByLabel('lookback').fill(String(fixture.spec.lookback))
  await page.getByLabel('frequency').selectOption('0')
  const labels = ['训练开始', '训练结束', '验证开始', '验证结束', '留出测试开始', '留出测试结束']
  const fields = ['train_start', 'train_end', 'validation_start', 'validation_end', 'test_start', 'test_end']
  for (let index = 0; index < fields.length; index++) await page.getByLabel(labels[index], { exact: true }).fill(fixture.spec[fields[index]])
  await page.getByText('高级训练、成交与风险参数', { exact: true }).click()
  await page.getByLabel('训练步数').fill(String(fixture.spec.total_timesteps))
  for (const [label, key] of [['训练片段', 'episode_sessions'], ['每轮采样步数', 'n_steps'], ['批大小', 'batch_size'], ['每轮优化次数', 'n_epochs'], ['隐藏维度', 'hidden_size'], ['验证间隔', 'eval_every_timesteps']]) await page.getByLabel(label, { exact: true }).fill(String(fixture.spec[key]))
  await expect(page.getByRole('button', { name: '创建模型', exact: true })).toBeDisabled()
  await page.getByRole('button', { name: '检查运行能力', exact: true }).click()
  await expect(page.getByRole('button', { name: '创建模型', exact: true })).toBeEnabled()
  await page.getByRole('button', { name: '创建模型', exact: true }).click()
  await expect(page).toHaveURL(/\/models\/[^/?]+$/)
  const modelId = new URL(page.url()).pathname.split('/').pop()!
  await expect(page.getByText('待训练：尚无保存版本。首次训练成功后才能回测。')).toBeVisible()
  expect(posts).toHaveLength(1)
  expect(posts[0].path).toBe('/api/investing/models')
  expect(posts[0].body).toMatchObject({ basket_revision_ref: { basket_id: fixture.basket_id, revision: 1 },
    learning_definition_ref: learning.ref, trading_definition_ref: trading.ref })
  expect(posts[0].body.spec.backtest).not.toHaveProperty('symbols')

  async function train(label: string, expectedVersions: number) {
    await page.getByLabel('追加决策步数').fill('16')
    const response = page.waitForResponse(response => response.url().endsWith(`/models/${modelId}/train`) && response.request().method() === 'POST')
    await page.getByRole('button', { name: '检查续训能力', exact: true }).click()
    await expect(page.getByRole('button', { name: label, exact: true })).toBeEnabled()
    await page.getByRole('button', { name: label, exact: true }).click()
    expect((await response).ok()).toBeTruthy()
    await expect.poll(async () => {
      const detail = await (await page.request.get(`/api/investing/models/${modelId}`, { headers })).json()
      const failed = detail.sessions.find((job: { state: string }) => job.state === 'failed')
      if (failed) throw new Error(JSON.stringify(failed))
      return detail.version_count
    }, { timeout: 90000 }).toBe(expectedVersions)
    await page.getByRole('button', { name: '刷新模型', exact: true }).click()
    await page.getByRole('button', { name: '查看最新', exact: true }).click()
    await expect(page.getByRole('heading', { name: `训练历史 · ${expectedVersions} 次尝试`, exact: true })).toBeVisible()
  }
  await train('开始训练', 1)
  const firstVersion = new URL(await page.getByRole('link', { name: '使用此版本回测' }).getAttribute('href') || '', page.url()).searchParams.get('version')!
  await train('继续训练', 2)
  const detail = await (await page.request.get(`/api/investing/models/${modelId}`, { headers })).json()
  expect(detail.sessions[0].base_version_id).toBe(firstVersion)
  expect(detail.total_timesteps).toBe(32)
  await page.getByRole('link', { name: '使用此版本回测' }).last().click()
  await expect(page.getByRole('combobox', { name: '训练轮次', exact: true })).toHaveValue(firstVersion)
  await page.getByLabel('回测开始').fill(fixture.spec.validation_start)
  await page.getByLabel('回测结束').fill(fixture.spec.validation_end)
  await page.getByRole('combobox', { name: '评估用途' }).selectOption('development_replay')
  await page.getByRole('button', { name: '检查运行能力', exact: true }).click()
  await expect(page.getByRole('button', { name: '提交模型回测', exact: true })).toBeEnabled()
  const response = page.waitForResponse(response => response.url().endsWith(`/models/${modelId}/backtests`) && response.request().method() === 'POST')
  await page.getByRole('button', { name: '提交模型回测', exact: true }).click()
  const submitted = await response
  expect(submitted.ok()).toBeTruthy()
  expect(posts.at(-1)?.body.version_id).toBe(firstVersion)
  expect(posts.at(-1)?.body.spec.evaluation_purpose).toBe('development_replay')
  const job = await submitted.json()
  await expect.poll(async () => (await (await page.request.get(`/api/jobs/${job.id}`, { headers })).json()).state, { timeout: 90000 }).toBe('succeeded')
  await page.getByRole('button', { name: '刷新回测', exact: true }).click()
  await page.goto(`/investing?backtest=${job.id}`)
  const report = page.getByRole('region', { name: '日线回测结果' })
  await expect(report).toBeVisible()
  await expect(report.getByRole('heading', { name: /日线回测结果 · .+ · \d{4}-\d{2}-\d{2}—\d{4}-\d{2}-\d{2}/ })).toBeVisible()
  await expect(report.getByText(/标的组合：/)).toBeVisible()
  await expect(report.getByText(/基准 /)).toBeVisible()
  await expect(page.getByRole('button', { name: '下载完整 ZIP' })).toBeVisible()
  await expect(page.getByRole('button', { name: '提交训练' })).toHaveCount(0)
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto(`/models/${modelId}`)
  await expect(page.getByRole('heading', { name: 'E2E 合成模型', exact: true })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('model-mobile.png') })
  await page.goto('/models')
  await expect(page.getByRole('combobox', { name: '组合' })).toContainText(`Model E2E ${assetType}`)
  await expect(page.getByRole('combobox', { name: '组合' })).not.toContainText(fixture.basket_id)
  expect(errors).toEqual([])
})
