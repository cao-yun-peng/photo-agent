import { FeaturePage } from '@/features/shell/feature-page';

export default function SkillsPage() {
  return (
    <FeaturePage
      kicker="Creative recipes"
      title="Skill 广场"
      description="Skill 入口和路由已经建立。Phase 3 将接入广场、我的 Skill、编辑和生成确认。"
      cards={[
        { title: '官方与公开', description: '沿用广场接口展示官方 Skill 和用户公开的创作配方。' },
        { title: '自定义 Skill', description: '支持创建、编辑、公开范围和参考图管理。' },
        { title: '额度与确认', description: '每日额度、费用提示和安全确认仍以后端结果为准。' },
      ]}
    />
  );
}
