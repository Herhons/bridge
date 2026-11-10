// BRIDGE 0.0.9 后台 service worker。
// 这里只做一件事：把工具栏图标的行为设成「点一下直接开侧栏」。
// 主界面全部在 sidepanel.html，没有 popup，因此不存在 popup 与侧栏双开、
// 两份页面抢 storage.local 的竞争。（setPanelBehavior 会持久化，装完即生效。）
void chrome.sidePanel
  .setPanelBehavior({ openPanelOnActionClick: true })
  .catch(() => {});
