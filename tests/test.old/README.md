# 测试目录

本目录包含项目各模块的测试脚本。

## 测试文件列表

### 后端测试

#### test_vision_config.py
视觉配置测试脚本，测试视觉模型配置加载是否正确。

**运行方式:**
```bash
python test/test_vision_config.py
```

**测试内容:**
- 视觉模型配置加载
- 视觉模式检测

#### test_vision_api.py
视觉聊天 API 测试脚本，测试视觉聊天 API 端点。

**运行方式:**
```bash
# 需要先启动后端服务
python test/test_vision_api.py
```

**测试内容:**
- 配置端点响应
- 多模态消息格式

### 前端测试

#### test_vision_frontend.ts
前端视觉功能测试脚本，使用 Jest 测试框架。

**运行方式:**
```bash
npm test -- --testPathPattern=test_vision_frontend
# 或
npx jest test_vision_frontend.ts
```

**测试内容:**
- 视觉聊天消息构建
- 图片验证逻辑
- Token 估算

## 测试覆盖范围

| 模块 | 测试文件 | 覆盖内容 |
|------|----------|----------|
| 配置加载 | test_vision_config.py | 视觉配置解析 |
| API | test_vision_api.py | 端点响应、多模态消息 |
| 前端逻辑 | test_vision_frontend.ts | 消息构建、验证逻辑 |

## 运行所有测试

```bash
# 后端测试
python test/test_vision_config.py
python test/test_vision_api.py

# 前端测试
npm test -- --testPathPattern=test_vision_frontend
```

## 注意事项

1. 部分测试需要后端服务运行才能执行
2. 前端测试需要安装 Jest 相关依赖
3. 建议在 CI/CD 流程中集成测试
