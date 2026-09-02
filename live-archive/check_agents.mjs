import { AgentDispatchClient, RoomServiceClient } from 'livekit-server-sdk';
import fs from 'fs';

const lk = JSON.parse(fs.readFileSync('/home/zman/zorc/live-archive/secrets/livekit.json', 'utf8'));
const roomService = new RoomServiceClient(lk.url, lk.api_key, lk.api_secret);
const agentDispatch = new AgentDispatchClient(lk.url, lk.api_key, lk.api_secret);

const rooms = await roomService.listRooms();
console.log('Active rooms:', rooms.map(r => r.name));

for (const r of rooms) {
  try {
    const dispatches = await agentDispatch.listDispatch(r.name);
    console.log(r.name, '-> dispatches:', JSON.stringify(dispatches));
  } catch (e) {
    console.log(r.name, '-> dispatch check failed:', e.message);
  }
}
