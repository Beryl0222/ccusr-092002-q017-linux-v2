# 运动处方安全护栏

帮助临床团队开具超慢跑等低冲击运动处方并持续识别禁忌与异常。

`contracts/prescription_case.json` 保存公开的领域样例，用来约定外部数据的名称与层级；样例不含真实个人资料、业务凭据或生产连接信息。

执行 `python3 service.py --check` 可检查服务身份，运行 `python3 -m unittest discover -s tests -v` 可核对基础契约。服务启动后，`/health` 返回项目标识。
