"""Keep public shop openings on a recorded path without exposing future entries."""
import hashlib
import json
from pathlib import Path


class RecordedShopSchedule:
    def __init__(self, replay, *, source, source_sha256):
        frames=replay['steps']
        if len(frames)!=720:
            raise ValueError('A shop schedule requires all720 original frames')
        self.rows=[]
        previous=[]
        interval=int(replay['configuration']['townShopUnlockInterval'])*int(replay['configuration']['turnsPerDay'])
        for tick,frame in enumerate(frames):
            row=frame[0]['observation']['town']['unlocked_shops']
            if row!=frame[1]['observation']['town']['unlocked_shops']:
                raise ValueError('Both seats must observe the same public shops')
            if not isinstance(row,list) or any(not isinstance(shop,str) for shop in row):
                raise ValueError('Malformed public shop list')
            if tick and row!=previous:
                if tick%interval or len(row)!=len(previous)+1 or row[:-1]!=previous:
                    raise ValueError('Shop openings must preserve order and opening time')
            self.rows.append(tuple(row))
            previous=row
        self.source=str(Path(source).resolve())
        self.source_sha256=source_sha256
        self.schedule_sha256=hashlib.sha256(json.dumps(self.rows,separators=(',',':')).encode()).hexdigest()
        self.overrides=0

    def attach(self,rules,turns_per_day):
        original=rules._end_of_day
        if any(shop not in rules.SHOPS for row in self.rows for shop in row):
            raise ValueError('Recorded schedule contains an unknown shop')
        def end_of_day(states,env,day):
            original(states,env,day)
            tick=(day+1)*turns_per_day
            expected=self.rows[tick]
            if tuple(states[0].observation.town['unlocked_shops'])!=expected:
                self.overrides+=1
            states[0].observation.town['unlocked_shops']=list(expected)
        rules._end_of_day=end_of_day
        return original

    def verify_frame(self,states,tick):
        for side in (0,1):
            if tuple(states[side]['observation']['town']['unlocked_shops'])!=self.rows[tick]:
                raise AssertionError(('SHOP_SCHEDULE_MISMATCH',tick,side))

    def report(self):
        return dict(policy='fixed_recorded',source=self.source,source_sha256=self.source_sha256,
                    schedule_sha256=self.schedule_sha256,verified_frames=720,changed_native_draws=self.overrides,
                    implementation_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    boundary='Only shop openings are fixed; inventory, prices, actions and native weed rules remain dynamic. Each policy sees only the already opened shops.')
