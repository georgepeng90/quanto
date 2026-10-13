import { expect, test } from '@playwright/test'
import { execFileSync } from 'node:child_process'
import { resolve } from 'node:path'

test('screening handoff preserves basket and missing stock fees block creation without training', async ({ page }, testInfo) => {
  const root = resolve(import.meta.dirname, '../..')
  const python = resolve(root, process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python')
  const fixture = JSON.parse(execFileSync(python, ['-c', `
import json, sys
from pathlib import Path
from quanto.workbench.store import WorkbenchStore
from quanto.backtest.dataset_loaders import load_dataset
from quanto.backtest.dataset_identity import make_import_evidence
from tests.fixtures.stock_training_cases import stock_case, stock_binding
from tests.fixtures.screening_cases import screening_request
workspace = Path(sys.argv[1])
source = workspace / 'screening-e2e-data'
spec, _ = stock_case(source)
binding = stock_binding(load_dataset(source))
manifest = json.loads((source / 'manifest.json').read_text())
del manifest['auxiliary_tables']['fee_rules']
(source / 'manifest.json').write_text(json.dumps(manifest))
evidence = make_import_evidence(source, assets=binding.assets, benchmark=binding.benchmark,
 source_document='Synthetic missing fee evidence', table_semantics=tuple(s for s in binding.table_semantics if s.table != 'fee_rules'))
dataset = WorkbenchStore(workspace).register_dataset(source, identity_evidence=evidence)
print(json.dumps({'request': screening_request(), 'dataset_id': dataset.id, 'spec': spec}))
`, process.env.QUANTO_E2E_WORKSPACE!], { cwd: root, encoding: 'utf8' }))
  const errors: string[] = [], training: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('request', request => { if (request.method() === 'POST' && request.url().endsWith('/train')) training.push(request.url()) })
  const { token } = await (await page.request.get('/api/session')).json()
  const headers = { Authorization: `Bearer ${token}` }
  const submitted = await page.request.post('/api/screenings', { headers, data: fixture.request })
  expect(submitted.ok()).toBeTruthy()
  const record = await submitted.json()
  await page.goto('/screenings')
  await page.getByRole('link', { name: fixture.request.name }).click()
  await expect(page.getByRole('heading', { name: fixture.request.name })).toBeVisible()
  await page.setViewportSize({ width: 390, height: 844 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('screening-mobile.png') })
  await page.getByRole('button', { name: '用于创建个股模型' }).click()
  await expect(page).toHaveURL(/\/models\?basket=/)
  const basketId = new URL(page.url()).searchParams.get('basket')!
  await expect(page.getByLabel('标的组合')).toHaveValue(basketId)
  await expect(page.getByText(/组合成员/)).toContainText('SZSE:stock:000001')
  const basket = await (await page.request.get(`/api/baskets/${basketId}`, { headers })).json()
  expect(basket.current.selection_origin.screening_id).toBe(record.id)
  await page.getByLabel('日线数据版本').selectOption(fixture.dataset_id)
  await page.getByLabel('学习定义').selectOption(JSON.stringify(['learning', 'stock-ppo-cash-log', '1']))
  await page.getByLabel('交易定义').selectOption(JSON.stringify(['trading', 'stock-daily', '1']))
  await page.getByLabel('模型名称').fill('缺费率证据的模型')
  await page.getByLabel('lookback').fill(String(fixture.spec.lookback))
  await page.getByLabel('frequency').selectOption('0')
  for (const [label, field] of [['训练开始', 'train_start'], ['训练结束', 'train_end'], ['验证开始', 'validation_start'], ['验证结束', 'validation_end']]) await page.getByLabel(label, { exact: true }).fill(fixture.spec[field])
  await page.getByRole('button', { name: '检查运行能力' }).click()
  await expect(page.getByRole('region', { name: '能力检查报告' })).toContainText('服务端检查：已阻止')
  await expect(page.getByRole('region', { name: '能力检查报告' })).toContainText('fee_rules')
  await expect(page.getByRole('button', { name: '创建模型', exact: true })).toBeDisabled()
  expect(training).toEqual([])
  expect(errors).toEqual([])
})
