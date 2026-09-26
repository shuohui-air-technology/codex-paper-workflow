import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { test, expect } from './fixtures';

test('capture the README screenshot from the real local Studio', async ({ studio }) => {
  const { page } = studio;
  await expect(page.getByText('官方流程 v1.0')).toBeVisible();
  await page.getByRole('button', { name: '复制为自定义流程' }).click();
  await expect(page.getByText('已在浏览器中创建草稿；保存前不会更改项目文件。')).toBeVisible();
  await expect(page.getByRole('region', { name: '可视化工作流图' })).toBeVisible();
  await expect(page.getByRole('complementary', { name: '添加流程阶段' })).toBeVisible();
  await expect(page.getByRole('complementary', { name: '阶段属性' })).toBeVisible();
  await expect(page.getByRole('heading', { name: '流程检查' })).toBeVisible();

  const output = resolve(process.cwd(), '..', 'assets', 'workflow-studio.png');
  mkdirSync(resolve(output, '..'), { recursive: true });
  await page.screenshot({ path: output, animations: 'disabled' });
});
