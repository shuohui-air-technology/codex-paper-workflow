import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { test, expect } from './fixtures';

test('capture the README screenshot from the real local Studio', async ({ studio }) => {
  const { page } = studio;
  await expect(page.getByText('官方流程 v1.0')).toBeVisible();
  await page.getByRole('button', { name: '新建空白流程' }).click();
  await expect(page.getByText('已在浏览器中创建草稿；保存前不会更改项目文件。')).toBeVisible();
  await page.getByLabel('阶段名称').fill('研究问题梳理');
  await page.getByLabel('主要 Skill').selectOption('research-skill-router');
  await page.getByRole('button', { name: /空白任务阶段/ }).click();
  await page.getByLabel('阶段名称').fill('文献发现');
  await page.getByLabel('主要 Skill').selectOption('research-skill-router');
  await page.getByRole('button', { name: /空白任务阶段/ }).click();
  await page.getByLabel('阶段名称').fill('论文结构与写作');
  await page.getByLabel('主要 Skill').selectOption('research-skill-router');
  await page.getByRole('button', { name: '自动整理画布' }).click();
  await expect(page.getByRole('region', { name: '可视化工作流图' })).toBeVisible();
  await expect(page.getByText('官方流程映射说明')).toHaveCount(0);
  await expect(page.getByRole('complementary', { name: '添加流程阶段' })).toBeVisible();
  await expect(page.getByRole('complementary', { name: '阶段属性' })).toBeVisible();
  await expect(page.getByRole('heading', { name: '流程检查' })).toBeVisible();
  const canvasControls = page.locator('.react-flow__controls-button');
  await expect(canvasControls).toHaveCount(3);
  await canvasControls.nth(2).click();
  await page.locator('.inspector-scroll').evaluate((element) => { element.scrollTop = 0; });

  const output = resolve(process.cwd(), '..', 'assets', 'workflow-studio.png');
  mkdirSync(resolve(output, '..'), { recursive: true });
  await page.screenshot({ path: output, animations: 'disabled' });
});
