# bili-downloader
一个极简的B站视频下载器demo（带GUI）

> - 仅用于学习，功能毕竟比较敏感，所以不打算打包Exe，<del>感兴趣可以自学python</del>
> - 至于登录功能主要是太危险了所以不做，但是可以在设置里手动填写cookie

**优点**：
1. 因为是开源的，无广告
2. 轻量，没那么多乱七八糟的功能
3. 因为是开源的，能不断维护更新
4. 因为是开源的，下面👇列出的所有缺点都可以靠自己改代码解决

**缺点**：
1. 可能会由于网络问题出bug（目前最新版我自己用的已经没啥问题了，但终究不敢说完全没bug了）
2. 暂时只能下载第一个分P（自己改代码 `get_download_url` 的参数可解，我主要是懒得改UI）
3. 下载文件的文件名、路径等无法自定义（自己改代码可解）
