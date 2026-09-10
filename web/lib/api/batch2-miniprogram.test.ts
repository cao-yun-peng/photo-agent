/* eslint-disable @typescript-eslint/no-explicit-any -- native wx/App callbacks have no bundled SDK types */
import { createRequire } from 'node:module';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createSseParser } from './agent-stream';
const require = createRequire(import.meta.url);
const mini = require('../../../miniprogram/utils/api.js');
afterEach(() => vi.unstubAllGlobals());

describe('shared SSE protocol fixtures', () => {
  for (const separator of ['\n','\r\n','\r']) {
    it('handles every byte split with '+JSON.stringify(separator), () => {
      const source = ': comment'+separator+'data: {"type":"final",'+separator+'data: "payload":{"message":"猫咪🌸"}}'+separator+separator+'data: {"type":"done","payload":{}}'+separator+separator;
      const bytes = new TextEncoder().encode(source);
      for (let split=0; split<=bytes.length; split++) {
        const actual: unknown[] = [], expected: unknown[] = [];
        const wxParser = mini._createSseParser((e: unknown)=>actual.push(e));
        const decoder = mini._createUtf8StreamDecoder();
        wxParser.feed(decoder.decode(bytes.slice(0,split)));
        wxParser.feed(decoder.decode(bytes.slice(split)));
        wxParser.feed(decoder.decode(null,true)); wxParser.flush();
        const parser = createSseParser(e=>expected.push(e));
        parser.feed(source); parser.flush();
        expect(actual).toEqual(expected);
        expect(actual).toHaveLength(2);
      }
    });
  }
  it('handles mixed line endings, errors and multiple frames', () => {
    for (const create of [mini._createSseParser, createSseParser]) {
      const events: {type:string}[] = [];
      const parser = create((event: {type:string})=>events.push(event));
      for (const char of 'data: {"type":"error","payload":{}}\r\n\ndata: {"type":"done","payload":{}}\r\r') parser.feed(char);
      parser.flush();
      expect(events.map(e=>e.type)).toEqual(['error','done']);
    }
  });
});

function setup() {
  let app: any; // native mini-program globals are intentionally mocked
  let request: any;
  vi.stubGlobal('App',(value: unknown)=>{app=value;});
  vi.stubGlobal('getApp',()=>app);
  vi.stubGlobal('wx',{getStorageSync:vi.fn(),setStorageSync:vi.fn(),removeStorageSync:vi.fn(),reLaunch:vi.fn(),
    request:vi.fn((options: unknown)=>{request=options;return {onChunkReceived:vi.fn(),abort:vi.fn()};})});
  delete require.cache[require.resolve('../../../miniprogram/app.js')];
  require('../../../miniprogram/app.js');
  return {app, response:(statusCode:number,data={})=>request.success({statusCode,data,header:{}})};
}
describe('mini-program authentication',()=>{
  for (const stream of [false,true]) {
    it('rejects expired credentials and protects a newer session '+stream,async()=>{
      const {app,response}=setup();
      app.setLogin({token:'A',user:{id:'A'}});
      const pending=stream?mini.agent.stream({query:'cat'}):mini.auth.me();
      app.setLogin({token:'B',user:{id:'B'}});
      response(401);
      await expect(pending).rejects.toMatchObject({status:401});
      expect(app.globalData.token).toBe('B');
      const current=stream?mini.agent.stream({query:'cat'}):mini.auth.me();
      response(401);
      await expect(current).rejects.toMatchObject({status:401});
      expect(app.globalData.token).toBeNull();
      expect(app.globalData.user).toBeNull();
    });
  }
  it('validates stored credentials at launch',async()=>{
    const {app,response}=setup();
    (globalThis as any).wx.getStorageSync.mockReturnValue('stored');
    app.onLaunch();
    expect(app.isLoggedIn()).toBe(false);
    response(200,{id:'verified'});
    await app.authReady;
    expect(app.isLoggedIn()).toBe(true);
    expect(app.globalData.user.id).toBe('verified');
  });
});


it('new mini-program Skills use an allowed model and historical invalid values require selection', async()=>{
  setup();
  const wx = (globalThis as any).wx;
  wx.getFileSystemManager=()=>({});
  wx.showToast=vi.fn();
  wx.navigateBack=vi.fn();
  let page: any;
  vi.stubGlobal('Page',(value: unknown)=>{page=value;});
  delete require.cache[require.resolve('../../../miniprogram/pages/skill-edit/index.js')];
  require('../../../miniprogram/pages/skill-edit/index.js');
  page.setData=(value: unknown)=>Object.assign(page.data,value);
  page.data.name='test'; page.data.prompt_template='warm';
  const create=vi.spyOn(mini.skills,'create').mockResolvedValue({});
  vi.useFakeTimers();
  try {
    await page.onSave();
    expect(create).toHaveBeenCalledWith(expect.objectContaining({model:'wanx2.1-imageedit'}));
    const detail=vi.spyOn(mini.skills,'detail').mockResolvedValue({name:'old',prompt_template:'warm',model:'wanx-v1'});
    await page.loadSkill('old');
    expect(page.data.model).toBe('');
    await page.onSave();
    expect(create).toHaveBeenCalledTimes(1);
    detail.mockRestore();
  } finally {
    create.mockRestore(); vi.clearAllTimers(); vi.useRealTimers();
  }
});


it('mini-program appends batches, keeps selection, removes rejected photos and clears replaced goals', () => {
  setup();
  (globalThis as any).wx.getRecorderManager = () => ({});
  let page: any;
  vi.stubGlobal('Page', (value: unknown) => { page = value; });
  delete require.cache[require.resolve('../../../miniprogram/pages/search/index.js')];
  require('../../../miniprogram/pages/search/index.js');
  page.setData = (values: unknown) => Object.assign(page.data, values);
  const emit = (type: string, payload: unknown) => page._handleAgentEvent({ type, payload }, 'browse');
  emit('search_state', { display_mode: 'replace' });
  emit('tool_result', { tool: 'search_photos', result: { ok: true, display_mode: 'replace', items: [{ id: 'a' }], result_batch_id: 'first' } });
  page.data.selectedResultId = 'a';
  emit('tool_result', { tool: 'continue_search', result: { ok: true, display_mode: 'append', items: [{ id: 'a' }, { id: 'b' }], result_batch_id: 'second' } });
  expect(page.data.results.map((p: any) => p.id)).toEqual(['a', 'b']);
  expect(page.data.selectedResultId).toBe('a');
  emit('tool_result', { tool: 'continue_search', result: { ok: true, display_mode: 'append', items: [] } });
  expect(page.data.feedbackBatchId).toBe('second');
  emit('feedback', { removed_photo_ids: ['a'] });
  expect(page.data.results.map((p: any) => p.id)).toEqual(['b']);
  expect(page.data.selectedResultId).toBe('');
  emit('search_state', { display_mode: 'replace' });
  emit('tool_result', { tool: 'search_photos', result: { ok: false } });
  expect(page.data.results).toEqual([]);
});


it('mini-program buttons reject exact photos and undo restores order and selection', async () => {
  setup();
  (globalThis as any).wx.getRecorderManager = () => ({});
  let page: any;
  vi.stubGlobal('Page', (value: unknown) => { page = value; });
  delete require.cache[require.resolve('../../../miniprogram/pages/search/index.js')];
  require('../../../miniprogram/pages/search/index.js');
  page.setData = (values: unknown) => Object.assign(page.data, values);
  const emit = (type: string, payload: unknown) => page._handleAgentEvent({ type, payload }, 'browse');
  emit('search_state', { display_mode: 'replace' });
  emit('tool_result', { tool: 'search_photos', result: { ok: true, items: [{ id: 'a' }, { id: 'b' }], result_batch_id: 'batch', result_batch_number: 1 } });
  page.data.agentSessionId = 'session'; page.data.selectedResultId = 'a';
  const stream = vi.spyOn(mini.agent, 'stream').mockImplementation(async (req: any) => {
    if (req.ui_action.action === 'reject_photo') emit('feedback', { removed_photo_ids: ['a'], undo_id: 'undo' });
    else emit('feedback_undone', { items: [{ id: 'a', batch_number: 1, batch_position: 1, result_batch_id: 'batch' }], selected_photo_id: 'a' });
  });
  try {
    await page.onRejectResult({ currentTarget: { dataset: { index: 0 } } });
    expect((stream.mock.calls[0][0] as any).ui_action).toEqual({ action: 'reject_photo', photo_id: 'a', batch_id: 'batch' });
    expect(page.data.results.map((p: any) => p.id)).toEqual(['b']);
    expect(page.data.selectedResultId).toBe('');
    await page.onUndoFeedback();
    expect((stream.mock.calls[1][0] as any).ui_action).toEqual({ action: 'undo_feedback', undo_id: 'undo' });
    expect(page.data.results.map((p: any) => p.id)).toEqual(['a', 'b']);
    expect(page.data.selectedResultId).toBe('a');
    expect(page.data.undoId).toBe('');
  } finally { stream.mockRestore(); }
});
