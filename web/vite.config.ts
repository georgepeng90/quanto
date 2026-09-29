import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import { createHash } from 'node:crypto'
import { existsSync, readdirSync, readFileSync, writeFileSync } from 'node:fs'
import { resolve, relative } from 'node:path'

const web = import.meta.dirname
function files(directory: string): string[] {
  return existsSync(directory) ? readdirSync(directory, { withFileTypes: true }).flatMap(entry => entry.isDirectory() ? files(resolve(directory, entry.name)) : [resolve(directory, entry.name)]) : []
}
function hashes(paths: string[], root: string) {
  return Object.fromEntries(paths.map(path => [relative(root, path).replaceAll('\\', '/'), createHash('sha256').update(readFileSync(path)).digest('hex')]))
}

export default defineConfig({
  plugins: [react(), { name: 'checkout-build-manifest', apply: 'build', closeBundle() {
    const dist = resolve(web, 'dist')
    const sources = hashes([...['package.json', 'package-lock.json', 'index.html', 'vite.config.ts', 'tsconfig.json'].map(name => resolve(web, name)), ...files(resolve(web, 'src')), ...files(resolve(web, 'public'))], web)
    writeFileSync(resolve(dist, 'build-manifest.json'), JSON.stringify({ sources, outputs: hashes(files(dist).filter(path => !path.endsWith('build-manifest.json')), dist) }))
  } }],
  server: { proxy: { '/api': 'http://127.0.0.1:8000' } },
  test: { environment: 'jsdom', setupFiles: './tests/setup.ts', exclude: ['e2e/**', 'node_modules/**'] }
})
