# Selene-TV 订阅

这是给 Selene-TV 本地模式使用的静态订阅文件。

## 订阅地址

直接在 Selene-TV 中填：

`https://raw.githubusercontent.com/clairvoyant1218/fnos-docker-deployer/selene-subscription/selene/subscription.txt`

## 当前内容

当前只收录公开直播源：

- 中国频道：iptv-org `countries/cn.m3u`
- 日本频道：iptv-org `countries/jp.m3u`
- 动画频道：iptv-org `categories/animation.m3u`
- 纪录片频道：iptv-org `categories/documentary.m3u`
- 新闻频道：iptv-org `categories/news.m3u`

`source.json` 是 Base58 编码前的可读配置；`subscription.txt` 是 Selene-TV 实际读取的 Base58 内容。

## 说明

Selene-TV 订阅要求 URL 返回 Base58 编码后的 JSON，结构包含 `api_site` 和/或 `lives`。当前版本暂不加入来源不明的影视点播接口，避免把不稳定或权属不清的站点打包进公开仓库。
