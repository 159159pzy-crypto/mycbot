export const VIEWS = {
  status: { title: '系统状态', subtitle: '智能体运行所依赖服务的实时就绪信号' },
  overview: { title: '数据总览', subtitle: '最近 14 天的用量、队列与轮次结果' },
  conversations: { title: '会话', subtitle: '各平台会话的消息记录与轮次明细' },
  sandbox: { title: '沙盒', subtitle: '通过真实消息链路验证文本、图片与追踪数据' },
  memories: { title: '记忆', subtitle: '长期记忆的范围、隐私与撤销管理' },
  plugins: { title: '插件', subtitle: '插件注册状态与工具审批' },
  models: { title: '模型', subtitle: '模型路由、连接状态与近 30 天用量' },
  persona: { title: '人设', subtitle: '智能体的系统提示词' },
} as const;

export type ViewId = keyof typeof VIEWS;
