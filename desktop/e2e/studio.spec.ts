import { connect, expect, node, SIMPLE_FLOW, test, waitForRunToFinish } from './fixtures';

test.describe('pipelines list', () => {
  test('shows pipelines with their summary', async ({ studio }) => {
    studio.writePipeline('demo.yaml', SIMPLE_FLOW);
    studio.writePipeline(
      'nightly.yaml',
      'name: Nightly backup\nsteps:\n  - {id: a, type: shell, command: echo a}\nschedule: {frequency: daily, at: "02:00"}\n',
    );
    await studio.open();
    const card = studio.page.locator('.card', { hasText: 'Demo flow' });
    await expect(card).toContainText('2 steps');
    await expect(card).toContainText('Never run');
    await expect(studio.page.locator('.card', { hasText: 'Nightly backup' })).toContainText('Daily at 02:00');
  });

  test('notices a YAML file copied into the folder while it is open', async ({ studio }) => {
    await studio.open();
    await expect(studio.page.getByText('No pipelines yet')).toBeVisible();
    studio.writePipeline('dropped.yaml', SIMPLE_FLOW.replace('Demo flow', 'Dropped in'));
    await expect(studio.page.locator('.card', { hasText: 'Dropped in' })).toBeVisible({ timeout: 15_000 });
  });

  test('flags an invalid pipeline instead of hiding it', async ({ studio }) => {
    studio.writePipeline('broken.yaml', 'name: Broken\nsteps:\n  - {id: x, type: nope}\n');
    await studio.open();
    const card = studio.page.locator('.card', { hasText: 'Broken' });
    await expect(card).toContainText('Has configuration errors');
    await expect(card.getByRole('button', { name: 'Run' })).toBeDisabled();
  });

  test('creates a pipeline and opens its canvas', async ({ studio }) => {
    await studio.open();
    await studio.page.getByRole('button', { name: 'New pipeline' }).first().click();
    await studio.page.getByLabel('Pipeline name').fill('Mi primer flujo');
    await studio.page.getByRole('button', { name: 'Create', exact: true }).click();
    await expect(studio.page).toHaveURL(/#\/editor\/mi-primer-flujo\.yaml/);
    await expect(node(studio.page, 'hello')).toBeVisible();
    expect(studio.readPipeline('mi-primer-flujo.yaml')).toContain('Mi primer flujo');
  });

  test('deletes a pipeline after confirmation (to the trash, not for good)', async ({ studio }) => {
    studio.writePipeline('demo.yaml', SIMPLE_FLOW);
    await studio.open();
    await studio.page.getByRole('button', { name: 'Delete' }).click();
    await studio.page.getByRole('dialog').getByRole('button', { name: 'Delete' }).click();
    await expect(studio.page.getByText('No pipelines yet')).toBeVisible();
  });
});

test.describe('canvas editing', () => {
  test.beforeEach(async ({ studio }) => {
    studio.writePipeline('demo.yaml', SIMPLE_FLOW);
    await studio.open('#/editor/demo.yaml');
    await expect(node(studio.page, 'extract')).toBeVisible();
    await expect(node(studio.page, 'publish')).toBeVisible();
  });

  test('adds a step, wires it with a failure condition and saves valid YAML', async ({ studio }) => {
    const { page } = studio;
    await page.getByRole('button', { name: 'Command step' }).click();
    await expect(node(page, 'shell')).toBeVisible();
    await page.locator('.inspector textarea').fill('echo alert');

    await connect(page, 'publish', 'shell');
    await expect(page.locator('.react-flow__edge')).toHaveCount(2);
    await page.locator('.react-flow__edge').last().click({ force: true });
    await page.getByText('On failure', { exact: true }).click();

    await expect(page.locator('.dirty')).toBeVisible();
    await page.keyboard.press('Control+s');
    await expect(page.getByText('Saved', { exact: true })).toBeVisible();
    await expect(page.locator('.dirty')).toHaveCount(0);

    const yaml = studio.readPipeline('demo.yaml');
    expect(yaml).toContain('echo alert');
    expect(yaml).toMatch(/step: publish/);
    expect(yaml).toMatch(/when: failure/);
    expect(yaml).toContain('ui:');

    await page.reload();
    await expect(node(page, 'shell')).toBeVisible();
    await expect(page.locator('.react-flow__edge')).toHaveCount(2);
  });

  test('refuses to save an incomplete step and leaves the file untouched', async ({ studio }) => {
    const { page } = studio;
    const before = studio.readPipeline('demo.yaml');
    await page.getByRole('button', { name: 'Command step' }).click();
    await expect(page.getByText('needs a command')).toBeVisible();
    await page.keyboard.press('Control+s');
    await expect(page.getByText('Fix the highlighted problems before saving.')).toBeVisible();
    expect(studio.readPipeline('demo.yaml')).toBe(before);
  });

  test('refuses a connection that would create a loop', async ({ studio }) => {
    await connect(studio.page, 'publish', 'extract');
    await expect(studio.page.getByText('That connection would create a loop.')).toBeVisible();
    await expect(studio.page.locator('.react-flow__edge')).toHaveCount(1);
  });

  test('undo and redo walk back through edits', async ({ studio }) => {
    const { page } = studio;
    await node(page, 'publish').click();
    await page.keyboard.press('Delete');
    await expect(page.locator('.react-flow__node')).toHaveCount(1);
    await page.getByRole('button', { name: 'Undo' }).click();
    await expect(page.locator('.react-flow__node')).toHaveCount(2);
    await page.getByRole('button', { name: 'Redo' }).click();
    await expect(page.locator('.react-flow__node')).toHaveCount(1);
  });

  test('renames a step and keeps its connections', async ({ studio }) => {
    const { page } = studio;
    await node(page, 'publish').click();
    const id = page.getByLabel('Identifier');
    await id.fill('release');
    await id.press('Enter');
    await expect(node(page, 'release')).toBeVisible();
    await expect(page.locator('.react-flow__edge')).toHaveCount(1);
    await id.fill('extract'); // already taken
    await id.press('Enter');
    await expect(page.getByText('Another step already uses that identifier.')).toBeVisible();
  });

  test('keeps node positions after dragging', async ({ studio }) => {
    const { page } = studio;
    const target = node(page, 'publish');
    const box = (await target.boundingBox())!;
    await page.mouse.move(box.x + box.width / 2, box.y + 12);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width / 2, box.y + 150, { steps: 8 });
    await page.mouse.up();
    await page.keyboard.press('Control+s');
    await expect(page.getByText('Saved', { exact: true })).toBeVisible();
    expect(studio.readPipeline('demo.yaml')).toMatch(/publish: \[\d+, \d+\]/);
  });

  test('warns before leaving with unsaved changes', async ({ studio }) => {
    const { page } = studio;
    await page.getByLabel('Name', { exact: true }).fill('Renamed flow');
    await page.getByRole('link', { name: 'Runs' }).click();
    const dialog = page.getByRole('dialog', { name: 'Unsaved changes' });
    await expect(dialog).toBeVisible();
    await dialog.getByRole('button', { name: 'Cancel' }).click();
    await expect(page).toHaveURL(/#\/editor/);
    await page.getByRole('link', { name: 'Runs' }).click();
    await dialog.getByRole('button', { name: 'Save and continue' }).click();
    await expect(page).toHaveURL(/#\/runs/);
    expect(studio.readPipeline('demo.yaml')).toContain('Renamed flow');
  });

  test('picks up an outside edit when clean, and flags a conflict when dirty', async ({ studio }) => {
    const { page } = studio;
    studio.writePipeline('demo.yaml', SIMPLE_FLOW.replace('Demo flow', 'Edited elsewhere'));
    await expect(page.getByText('The file changed on disk and was reloaded.')).toBeVisible({ timeout: 15_000 });
    await expect(page.locator('.editor-title strong')).toHaveText('Edited elsewhere');

    await page.getByLabel('Name', { exact: true }).fill('My local change');
    studio.writePipeline('demo.yaml', SIMPLE_FLOW.replace('Demo flow', 'Edited again'));
    await expect(page.getByText('This file changed on disk after you opened it.')).toBeVisible({ timeout: 15_000 });
    await page.getByRole('button', { name: 'Overwrite with my version' }).click();
    await expect.poll(() => studio.readPipeline('demo.yaml')).toContain('My local change');
  });
});

test.describe('running', () => {
  test('shows live status and logs, then the finished run', async ({ studio }) => {
    const { page } = studio;
    studio.writePipeline('demo.yaml', SIMPLE_FLOW);
    await studio.open('#/editor/demo.yaml');
    await page.getByRole('button', { name: 'Run', exact: true }).click();

    await expect(node(page, 'extract').locator('.step-node')).toHaveClass(/ns-(running|success)/);
    await expect(node(page, 'publish').locator('.step-node')).toHaveClass(/ns-success/, { timeout: 30_000 });
    await expect(page.locator('.log-row', { hasText: 'starting' })).toBeVisible();
    await expect(page.locator('.log-row', { hasText: 'careful' })).toContainText('WARNING');
    await expect(page.locator('.log-row.lv-stderr', { hasText: 'oops' })).toBeVisible();
    await waitForRunToFinish(studio);

    await page.getByRole('link', { name: 'Runs' }).click();
    await expect(page.locator('tbody tr').first()).toContainText('Succeeded');
    await page.locator('tbody a.name').first().click();
    await expect(page.locator('.tl-row')).toHaveCount(2);
    await expect(page.locator('.log-row', { hasText: 'published' })).toBeVisible();
  });

  test('runs the recovery branch when a step fails', async ({ studio }) => {
    const { page } = studio;
    studio.writePipeline(
      'recovery.yaml',
      `name: Recovery
steps:
  - {id: work, type: python, script: scripts/fail.py}
  - {id: alert, type: shell, command: echo alerted, needs: [{step: work, when: failure}]}
  - {id: publish, type: shell, command: echo published, needs: [work]}
`,
    );
    await studio.open('#/editor/recovery.yaml');
    await page.getByRole('button', { name: 'Run', exact: true }).click();
    await expect(node(page, 'alert').locator('.step-node')).toHaveClass(/ns-success/, { timeout: 30_000 });
    await expect(node(page, 'work').locator('.step-node')).toHaveClass(/ns-failed/);
    await expect(node(page, 'publish').locator('.step-node')).toHaveClass(/ns-skipped/);
    await waitForRunToFinish(studio);
    await page.getByRole('link', { name: 'Runs' }).click();
    await expect(page.locator('tbody tr').first()).toContainText('Failed');
  });

  test('runs a single selected step', async ({ studio }) => {
    const { page } = studio;
    studio.writePipeline('demo.yaml', SIMPLE_FLOW);
    await studio.open('#/editor/demo.yaml');
    await node(page, 'publish').click();
    await page.getByRole('button', { name: 'Run only this step' }).click();
    await expect(node(page, 'publish').locator('.step-node')).toHaveClass(/ns-success/, { timeout: 30_000 });
    await waitForRunToFinish(studio);
    const runs = await studio.api<{ run_id: string }[]>('GET', '/api/runs');
    const detail = await studio.api<{ steps: { step_id: string }[] }>('GET', `/api/runs/${runs[0]!.run_id}`);
    expect(detail.steps.map((s) => s.step_id)).toEqual(['publish']);
  });

  test('stops a running pipeline', async ({ studio }) => {
    const { page } = studio;
    studio.writePipeline('slow.yaml', 'name: Slow\nsteps:\n  - {id: wait, type: python, script: scripts/slow.py}\n');
    await studio.open('#/editor/slow.yaml');
    await page.getByRole('button', { name: 'Run', exact: true }).click();
    await expect(page.locator('.log-row', { hasText: 'slow start' })).toBeVisible();
    await page.getByRole('button', { name: 'Stop' }).click();
    await expect(page.getByRole('button', { name: 'Run', exact: true })).toBeVisible({ timeout: 20_000 });
    await page.getByRole('link', { name: 'Runs' }).click();
    await expect(page.locator('tbody tr').first()).toContainText('Cancelled');
  });

  test('filters and exports the logs of a run', async ({ studio }) => {
    const { page } = studio;
    studio.writePipeline('demo.yaml', SIMPLE_FLOW);
    await studio.open('#/editor/demo.yaml');
    await page.getByRole('button', { name: 'Run', exact: true }).click();
    await expect(page.locator('.log-row', { hasText: 'published' })).toBeVisible({ timeout: 30_000 });
    await waitForRunToFinish(studio);

    await page.getByRole('button', { name: 'WARNING', exact: true }).click();
    await expect(page.locator('.log-row')).toHaveCount(1);
    await expect(page.locator('.log-row')).toContainText('careful');
    await page.getByRole('button', { name: 'WARNING', exact: true }).click();

    await page.getByPlaceholder('Search in logs').fill('PUBL');
    await expect(page.locator('.log-row')).toHaveCount(1);
    await expect(page.locator('.log-row')).toContainText('published');

    await page.getByPlaceholder('Search in logs').fill('');
    await page.getByLabel('Step', { exact: true }).selectOption('extract');
    await expect(page.locator('.log-row')).toHaveCount(3);

    const csv = page.getByRole('link', { name: 'CSV' });
    const href = await csv.getAttribute('href');
    expect(href).toContain('/export?');
    const response = await fetch(`${studio.url}${href}`);
    expect(response.headers.get('content-type')).toContain('text/csv');
    expect(await response.text()).toContain('seq,ts,step,stream,level,message');
  });
});

test.describe('scheduling', () => {
  const DAILY = `name: Nightly
steps:
  - {id: a, type: shell, command: echo night}
schedule: {frequency: daily, at: "23:45"}
`;

  test('shows the next run and lets the user pause and resume it', async ({ studio }) => {
    studio.writePipeline('nightly.yaml', DAILY);
    await studio.open();
    const card = studio.page.locator('.card', { hasText: 'Nightly' });
    await expect(card).toContainText('Daily at 23:45');
    await expect(card.locator('.next-run')).toContainText('Next run');
    await expect(card.locator('.next-run')).toContainText('23:45');
    await expect(studio.page.locator('.stat', { hasText: 'Scheduled' })).toContainText('1');

    await card.getByRole('button', { name: 'Pause' }).click();
    await expect(card.locator('.next-run')).toContainText('Schedule paused');
    await expect(studio.page.locator('.stat', { hasText: 'Scheduled' })).toContainText('0');
    await card.getByRole('button', { name: 'Resume' }).click();
    await expect(card.locator('.next-run')).toContainText('Next run');
  });

  test('edits a schedule on the canvas and saves it to the YAML', async ({ studio }) => {
    const { page } = studio;
    studio.writePipeline('plain.yaml', 'name: Plain\nsteps:\n  - {id: a, type: shell, command: echo a}\n');
    await studio.open('#/editor/plain.yaml');
    await expect(node(page, 'a')).toBeVisible();
    await page.getByText('Run on a schedule', { exact: true }).click();
    await page.getByLabel('Repeat').selectOption('weekly');
    await page.getByRole('button', { name: 'Fri', exact: true }).click();
    await page.locator('input[type=time]').fill('06:15');
    await page.getByText('Run once if a run was missed', { exact: true }).click();
    await page.keyboard.press('Control+s');
    await expect(page.getByText('Saved', { exact: true })).toBeVisible();

    const yaml = studio.readPipeline('plain.yaml');
    expect(yaml).toMatch(/frequency: weekly/);
    expect(yaml).toMatch(/at: ["']?06:15/);
    expect(yaml).toMatch(/MON/);
    expect(yaml).toMatch(/FRI/);
    expect(yaml).toMatch(/catch_up: false/);

    await page.getByRole('link', { name: 'Pipelines' }).first().click();
    await expect(page.locator('.card', { hasText: 'Plain' }).locator('.next-run')).toContainText('Next run');
  });

  test('a startup schedule runs by itself when the engine starts', async ({ studio }) => {
    studio.writePipeline(
      'boot.yaml',
      'name: At startup\nsteps:\n  - {id: a, type: shell, command: echo booted}\nschedule: {frequency: startup}\n',
    );
    await studio.restartEngine(); // the scheduler fires startup pipelines once per start
    await studio.open('#/runs');
    const row = studio.page.locator('tbody tr', { hasText: 'At startup' });
    await expect(row).toContainText('Schedule', { timeout: 20_000 });
    await expect(row).toContainText('Succeeded');
  });

  test('a scheduled run that was missed while closed runs once when reopened', async ({ studio }) => {
    studio.writePipeline(
      'daily.yaml',
      'name: Daily report\nsteps:\n  - {id: a, type: shell, command: echo report}\nschedule: {frequency: daily, at: "00:00"}\n',
    );
    await studio.open();
    await expect(studio.page.locator('.card', { hasText: 'Daily report' })).toBeVisible();
    // Pretend the last firing was three days ago, then "open the app again".
    const state = `${studio.dataDir}/scheduler.json`;
    const threeDaysAgo = new Date(Date.now() - 3 * 86_400_000);
    const iso = new Date(threeDaysAgo.getTime() - threeDaysAgo.getTimezoneOffset() * 60000).toISOString().slice(0, 19);
    await studio.restartEngine();
    const fs = await import('node:fs');
    fs.writeFileSync(state, JSON.stringify({ jobs: { 'daily.yaml': { last_fire: iso, paused: false } } }));
    await studio.restartEngine();
    await studio.open('#/runs');
    await expect(studio.page.locator('tbody tr', { hasText: 'Daily report' })).toHaveCount(1, { timeout: 20_000 });
    await expect(studio.page.locator('tbody tr', { hasText: 'Daily report' })).toContainText('Schedule');
  });
});

test.describe('secrets', () => {
  test('stores a secret, injects it into a step and masks it in the logs', async ({ studio }) => {
    const { page } = studio;
    studio.writePipeline(
      'secure.yaml',
      `name: Secure
steps:
  - id: show
    type: python
    script: scripts/secret.py
    env: {T: '\${secret:API_TOKEN}'}
`,
    );
    await studio.open('#/secrets');
    const row = page.locator('tr', { hasText: 'API_TOKEN' });
    await expect(row).toContainText('Missing');
    await row.getByRole('button', { name: 'Set value' }).click();
    await page.getByRole('dialog').locator('input[type=password]').fill('super-secret-123');
    await page.getByRole('dialog').getByRole('button', { name: 'Save' }).click();
    await expect(row).toContainText('Set');
    await expect(page.locator('body')).not.toContainText('super-secret-123');

    await page.getByRole('link', { name: 'Pipelines' }).click();
    await page.getByRole('button', { name: 'Run' }).click();
    await waitForRunToFinish(studio);
    await page.getByRole('link', { name: 'Runs' }).click();
    await page.locator('tbody a.name').first().click();
    await expect(page.locator('.log-row', { hasText: 'token is' })).toContainText('token is ***');
    await expect(page.locator('body')).not.toContainText('super-secret-123');
  });

  test('validates names and can delete a secret', async ({ studio }) => {
    const { page } = studio;
    await studio.open('#/secrets');
    await page.getByRole('button', { name: 'New secret' }).click();
    await page.getByLabel('Name').fill('1bad name');
    await expect(page.getByText('Use letters, digits and underscore')).toBeVisible();
    await page.getByLabel('Name').fill('GOOD_NAME');
    await page.getByRole('dialog').locator('input[type=password]').fill('value-1234');
    await page.getByRole('dialog').getByRole('button', { name: 'Save' }).click();
    const row = page.locator('tr', { hasText: 'GOOD_NAME' });
    await expect(row).toContainText('Not used');
    await row.getByRole('button', { name: 'Delete' }).click();
    await page.getByRole('dialog').getByRole('button', { name: 'Delete' }).click();
    await expect(page.getByText('No secrets')).toBeVisible();
  });
});

test.describe('settings', () => {
  test('switches the language and keeps it', async ({ studio }) => {
    const { page } = studio;
    await studio.open('#/settings');
    await page.getByLabel('Language').selectOption('es');
    await expect(page.getByRole('link', { name: 'Ejecuciones' })).toBeVisible();
    await page.reload();
    await expect(page.getByRole('link', { name: 'Ejecuciones' })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Ajustes' })).toBeVisible();
  });

  test('rejects a workspace folder that does not exist', async ({ studio }) => {
    const { page } = studio;
    await studio.open('#/settings');
    await page.getByRole('textbox', { name: 'Pipelines folder' }).fill('Z:/definitely/not/here');
    await page.getByRole('button', { name: 'Apply' }).first().click();
    await expect(page.getByText('is not an existing folder')).toBeVisible();
  });

  test('changing the workspace lists the other folder', async ({ studio }) => {
    const { page } = studio;
    studio.writePipeline('other/elsewhere.yaml', SIMPLE_FLOW.replace('Demo flow', 'Elsewhere'));
    await studio.open('#/settings');
    await page.getByRole('textbox', { name: 'Pipelines folder' }).fill(`${studio.workspace}/other`);
    await page.getByRole('button', { name: 'Apply' }).first().click();
    await page.getByRole('link', { name: 'Pipelines' }).click();
    await expect(page.locator('.card', { hasText: 'Elsewhere' })).toBeVisible();
  });
});

test.describe('resilience', () => {
  test('refuses to open without a token', async ({ studio }) => {
    await studio.page.goto(`${studio.url}/`);
    await expect(studio.page.getByText('Open OPR Studio from the app')).toBeVisible();
  });

  test('survives a crash-free reload while a run is in progress', async ({ studio }) => {
    const { page } = studio;
    studio.writePipeline('slow.yaml', 'name: Slow\nsteps:\n  - {id: wait, type: python, script: scripts/slow.py}\n');
    await studio.open('#/editor/slow.yaml');
    await page.getByRole('button', { name: 'Run', exact: true }).click();
    await expect(page.locator('.log-row', { hasText: 'slow start' })).toBeVisible();
    await page.reload();
    await expect(page.getByRole('button', { name: 'Stop' })).toBeVisible();
    await page.getByRole('button', { name: 'Stop' }).click();
    await expect(page.getByRole('button', { name: 'Run', exact: true })).toBeVisible({ timeout: 20_000 });
  });
});
