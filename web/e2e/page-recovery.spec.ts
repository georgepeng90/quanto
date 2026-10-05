import { test, expect } from '@playwright/test'

test('recovers a missing comparison page module by refreshing the current URL', async ({ page }) => {
  // A tab opened before a rebuild still requests the previous hashed module.
  await page.route('**/assets/ComparePage-*.js', route => route.fulfill({ status: 404, body: 'Not Found' }), { times: 1 })
  await page.goto('/runs')
  await page.getByRole('link', { name: '固定策略曲线对比', exact: true }).click()
  await expect(page.getByRole('heading', { name: '页面加载失败', exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Unexpected Application Error!' })).toHaveCount(0)
  await page.getByRole('button', { name: '刷新页面', exact: true }).click()
  await expect(page.getByRole('heading', { name: '运行对比', exact: true })).toBeVisible()
  await expect(page).toHaveURL('/compare')
})

test('preserves selected runs when refreshing a failed comparison page', async ({ page }) => {
  await page.route('**/assets/ComparePage-*.js', route => route.fulfill({ status: 404, body: 'Not Found' }), { times: 1 })
  await page.goto('/compare?run_ids=r1,r2')
  await expect(page.getByRole('heading', { name: '页面加载失败', exact: true })).toBeVisible()
  await page.getByRole('button', { name: '刷新页面', exact: true }).click()
  await expect(page.getByLabel('运行 ID（每行一个）')).toHaveValue('r1\nr2')
  await expect(page).toHaveURL('/compare?run_ids=r1,r2')
})
