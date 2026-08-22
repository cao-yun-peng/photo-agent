import { FeaturePage } from '@/features/shell/feature-page';

export default function GenerationsPage() {
  return (
    <FeaturePage
      kicker="Creation history"
      title="生成历史"
      description="生成历史路由已经进入统一应用壳。Phase 3 将接入任务轮询、状态恢复和结果预览。"
      cards={[
        { title: '幂等创建', description: '重复提交沿用服务端幂等键，不重复消耗生成额度。' },
        { title: '安全确认', description: 'awaiting_confirmation 状态必须带服务端确认令牌后才能入队。' },
        { title: '刷新恢复', description: '根据 generation id 恢复任务，不依赖页面内存保存状态。' },
      ]}
    />
  );
}
