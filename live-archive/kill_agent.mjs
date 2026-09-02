import { AgentDispatchClient } from 'livekit-server-sdk';
import fs from 'fs';
const lk = JSON.parse(fs.readFileSync('/home/zman/zorc/live-archive/secrets/livekit.json', 'utf8'));
const agentDispatch = new AgentDispatchClient(lk.url, lk.api_key, lk.api_secret);
await agentDispatch.deleteDispatch('AD_R7Y2rxnvtkHY', 'stream-1786358776087-4lim7');
console.log('Dispatch removed');
