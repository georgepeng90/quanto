import { test, expect } from '@playwright/test'
import { execFileSync } from 'node:child_process'
import { resolve } from 'node:path'

test('creates an untrained model, continues it, and backtests the chosen saved version', async ({ page }, testInfo) => {
  const root = resolve(import.meta.dirname, '../..')
  const python = resolve(root, process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python')
  const fixture = JSON.parse(execFileSync(python, ['-c', `
import json, sys
from pathlib import Path
from quanto.workbench.store import WorkbenchStore
from tests.fixtures.investing_training_cases import investing_case
workspace = Path(sys.argv[1])
assert workspace.resolve() != (Path.cwd() / 'workspace').resolve()
spec, _ = investing_case(workspace / 'model-e2e-data')
store = WorkbenchStore(workspace)
dataset = store.register_dataset(workspace / 'model-e2e-data')
print(json.dumps({'dataset_id': dataset.id, 'spec': spec}))
`, process.env.QUANTO_E2E_WORKSPACE!], { cwd: root, encoding: 'utf8' }))
  const posts: { path: string; body: any }[] = []
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('request', request => { if (request.method() === 'POST' && request.url().includes('/api/investing/')) posts.push({ path: new URL(request.url()).pathname, body: request.postDataJSON() }) })
  const session = await (await page.request.get('/api/session')).json()
  const headers = { Authorization: `Bearer ${session.token}` }
  await page.goto('/models')
  await expect(page.getByRole('navigation', { name: '主导航' }).getByRole('link')).toHaveText(['行情数据', '固定策略', '强化学习模型', '回测'])
  await page.getByRole('button', { name: '新建模型', exact: true }).click()
  await page.getByRole('combobox', { name: '日线数据版本', exact: true }).selectOption(fixture.dataset_id)
  await expect(page.getByLabel('固定 ETF 篮子（按顺序，逗号分隔）')).toHaveValue('')
  await page.getByLabel('模型名称').fill('E2E 合成模型')
  await page.getByLabel('固定 ETF 篮子（按顺序，逗号分隔）').fill('000001')
  const labels = ['训练开始', '训练结束', '验证开始', '验证结束', '测试开始', '测试结束']
  const fields = ['train_start', 'train_end', 'validation_start', 'validation_end', 'test_start', 'test_end']
  for (let index = 0; index < fields.length; index++) await page.getByLabel(labels[index], { exact: true }).fill(fixture.spec[fields[index]])
  await page.getByText('高级训练、成交与风险参数', { exact: true }).click()
  for (const [label, key] of [['历史窗口（交易日）', 'lookback'], ['训练片段（交易日）', 'episode_sessions'], ['训练决策步数', 'total_timesteps'], ['每轮采样步数', 'n_steps'], ['批大小', 'batch_size'], ['每轮优化次数', 'n_epochs'], ['隐藏维度', 'hidden_size'], ['验证间隔（决策步）', 'eval_every_timesteps']]) await page.getByLabel(label, { exact: true }).fill(String(fixture.spec[key]))
  await page.getByRole('button', { name: '创建模型', exact: true }).click()
  await expect(page).toHaveURL(/\/models\/[^/?]+$/)
  const modelId = new URL(page.url()).pathname.split('/').pop()!
  await expect(page.getByText('待训练：尚无保存版本。首次训练成功后才能回测。')).toBeVisible()
  expect(posts).toHaveLength(1)
  expect(posts[0].path).toBe('/api/investing/models')
  await expect(page.getByLabel('固定 ETF 篮子（按顺序，逗号分隔）')).toHaveCount(0)

  async function train(label: string, expectedVersions: number) {
    await page.getByLabel('追加决策步数').fill('16')
    const response = page.waitForResponse(response => response.url().endsWith(`/models/${modelId}/train`) && response.request().method() === 'POST')
    await page.getByRole('button', { name: label, exact: true }).click()
    expect((await response).ok()).toBeTruthy()
    await expect.poll(async () => {
      const detail = await (await page.request.get(`/api/investing/models/${modelId}`, { headers })).json()
      const failed = detail.sessions.find((job: { state: string }) => job.state === 'failed')
      if (failed) throw new Error(JSON.stringify(failed))
      return detail.version_count
    }, { timeout: 90000 }).toBe(expectedVersions)
    await page.getByRole('button', { name: '刷新模型', exact: true }).click()
    await expect(page.getByRole('heading', { name: `保存版本 · ${expectedVersions}`, exact: true })).toBeVisible()
  }
  await train('开始训练', 1)
  const firstVersion = new URL(await page.getByRole('link', { name: '使用此版本回测' }).getAttribute('href') || '', page.url()).searchParams.get('version')!
  await train('继续训练', 2)
  const detail = await (await page.request.get(`/api/investing/models/${modelId}`, { headers })).json()
  expect(detail.sessions[0].base_version_id).toBe(firstVersion)
  expect(detail.total_timesteps).toBe(32)
  await page.getByRole('link', { name: '使用此版本回测' }).last().click()
  await expect(page.getByRole('combobox', { name: '保存版本', exact: true })).toHaveValue(firstVersion)
  const response = page.waitForResponse(response => response.url().endsWith(`/models/${modelId}/backtests`) && response.request().method() === 'POST')
  await page.getByRole('button', { name: '提交模型回测', exact: true }).click()
  const submitted = await response
  expect(submitted.ok()).toBeTruthy()
  expect(posts.at(-1)?.body.version_id).toBe(firstVersion)
  const job = await submitted.json()
  await expect.poll(async () => (await (await page.request.get(`/api/jobs/${job.id}`, { headers })).json()).state, { timeout: 90000 }).toBe('succeeded')
  await page.getByRole('button', { name: '刷新回测', exact: true }).click()
  await page.goto(`/investing?backtest=${job.id}`)
  await expect(page.getByRole('region', { name: '日线回测结果' })).toBeVisible()
  await expect(page.getByRole('button', { name: '下载完整 ZIP' })).toBeVisible()
  await expect(page.getByRole('button', { name: '提交训练' })).toHaveCount(0)
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto(`/models/${modelId}`)
  await expect(page.getByRole('heading', { name: 'E2E 合成模型', exact: true })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('model-mobile.png') })
  expect(errors).toEqual([])
})
