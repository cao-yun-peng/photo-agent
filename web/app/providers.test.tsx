import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { useQuery } from '@tanstack/react-query';
import { afterEach, expect, it, vi } from 'vitest';
import { Providers } from './providers';
import { clearSession, readSession, saveSession } from '@/lib/auth/session';
import { apiClient } from '@/lib/api/client';
import { streamAgent } from '@/lib/api/agent-stream';
afterEach(()=>{cleanup();clearSession();vi.unstubAllGlobals();});

it('isolates every fixed user cache key and ignores late results across A to B',async()=>{
  saveSession('A',3600);
  const finish: ((value:string)=>void)[]=[];
  const keys=['auth/me','photos','quota','skills/private','generations'];
  function Data() {
    return <>{keys.map(key=><Row key={key} name={key}/>)}</>;
  }
  function Row({name}:{name:string}) {
    const query=useQuery({queryKey:[name],queryFn:()=>{
      const token=readSession()?.accessToken;
      return token==='A'?new Promise<string>(resolve=>finish.push(resolve)):Promise.resolve('B:'+name);
    }});
    return <p>{query.data||'loading'}</p>;
  }
  render(<Providers><Data/></Providers>);
  await waitFor(()=>expect(finish).toHaveLength(5));
  act(()=>{clearSession();saveSession('B',3600);});
  await screen.findByText('B:photos');
  await act(async()=>finish.forEach(resolve=>resolve('A:private')));
  for (const key of keys) expect(screen.getByText('B:'+key)).toBeInTheDocument();
  expect(screen.queryByText('A:private')).toBeNull();
});

it.each([false,true])('late 401 cannot clear B and old transport is aborted (SSE=%s)',async(stream)=>{
  saveSession('A',3600);
  let finish!: (value:Response)=>void;
  let signal: AbortSignal|undefined|null;
  vi.stubGlobal('fetch',vi.fn((input:Request,options?:RequestInit)=>{
    signal=options?.signal||input.signal;
    return new Promise<Response>(resolve=>{finish=resolve;});
  }));
  const pending=stream?streamAgent({query:'cat'}):apiClient.GET('/auth/me',{fetch:globalThis.fetch});
  await waitFor(()=>expect(finish).toBeTypeOf('function'));
  saveSession('B',3600);
  expect(signal?.aborted).toBe(true);
  finish(new Response(JSON.stringify({detail:'expired'}),{status:401,headers:{'Content-Type':'application/json'}}));
  await pending.catch(()=>{});
  expect(readSession()?.accessToken).toBe('B');
});
