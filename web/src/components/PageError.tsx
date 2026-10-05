export function PageError() {
  return <main className="main page-stack"><section className="panel" role="alert">
    <h1>页面加载失败</h1>
    <p>请刷新页面重试。如果工作台刚刚更新，刷新会加载最新版本。</p>
    <div className="inline-actions">
      <button type="button" onClick={() => window.location.reload()}>刷新页面</button>
      <a href="/runs">返回回测</a>
    </div>
  </section></main>
}
