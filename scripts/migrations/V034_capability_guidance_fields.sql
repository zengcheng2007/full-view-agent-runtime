-- V034: Add guidance fields to capability tables
-- Purpose: Enable API-driven capability guidance management
-- Issue: WSZC-14 - Tool/Skill/Workflow 统一能力管理架构设计

BEGIN;

-- Add guidance fields to capability_tools
ALTER TABLE full_view_agent.capability_tools
ADD COLUMN IF NOT EXISTS guidance TEXT DEFAULT '',
ADD COLUMN IF NOT EXISTS guidance_examples JSONB DEFAULT '[]'::jsonb,
ADD COLUMN IF NOT EXISTS display_order INTEGER;

-- Add display_order to capability_skills (guidance already exists)
ALTER TABLE full_view_agent.capability_skills
ADD COLUMN IF NOT EXISTS display_order INTEGER;

-- Add guidance and display_order to capability_workflows
ALTER TABLE full_view_agent.capability_workflows
ADD COLUMN IF NOT EXISTS guidance TEXT DEFAULT '',
ADD COLUMN IF NOT EXISTS display_order INTEGER;

-- Create indexes for display_order queries
CREATE INDEX IF NOT EXISTS idx_capability_tools_display_order
ON full_view_agent.capability_tools(display_order)
WHERE display_order IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_capability_skills_display_order
ON full_view_agent.capability_skills(display_order)
WHERE display_order IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_capability_workflows_display_order
ON full_view_agent.capability_workflows(display_order)
WHERE display_order IS NOT NULL;

-- Add comments for documentation
COMMENT ON COLUMN full_view_agent.capability_tools.guidance IS
'能力引导文本，告诉模型何时/如何使用该工具。发布时必填。';

COMMENT ON COLUMN full_view_agent.capability_tools.guidance_examples IS
'引导示例列表，JSON 格式：[{question, reasoning, expected_output}]';

COMMENT ON COLUMN full_view_agent.capability_tools.display_order IS
'在系统提示词中的排序权重，值越小越靠前';

COMMENT ON COLUMN full_view_agent.capability_skills.display_order IS
'在系统提示词中的排序权重，值越小越靠前';

COMMENT ON COLUMN full_view_agent.capability_workflows.guidance IS
'工作流引导文本，说明何时触发该工作流。发布时必填。';

COMMENT ON COLUMN full_view_agent.capability_workflows.display_order IS
'在系统提示词中的排序权重，值越小越靠前';

COMMIT;
