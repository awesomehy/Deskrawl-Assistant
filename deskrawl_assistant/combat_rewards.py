"""Keep a confirmed win separate from permission to leave its rewards behind."""
import time


class CompletionRewards:
    QUIET_SECONDS = 1.0

    def __init__(self):
        self.run_id = None
        self.location = None
        self.settled = False
        self.empty_since = None
        self.empty_reads = 0
        self.reason = ''

    def _reset_empty(self):
        self.empty_since = None
        self.empty_reads = 0

    def _sample_empty(self):
        now = time.monotonic()
        if self.empty_since is None:
            self.empty_since = now
        self.empty_reads += 1
        return self.empty_reads >= 2 and now - self.empty_since >= self.QUIET_SECONDS

    def _current_final_wave(self, scene):
        run = scene.get('run') or {}
        return (scene.get('in_dungeon') is True and scene.get('dead') is False
            and run.get('id') == self.run_id and not run.get('retained_terminal')
            and run.get('completion_confirmed') is True
            and run.get('wave_active') is False and run.get('enemy_count') == 0
            and type(run.get('wave_count')) is int and run['wave_count'] > 0
            and type(run.get('wave_index')) is int and run['wave_index'] >= run['wave_count'])

    def _final_kill_confirmed(self, scene):
        return self._current_final_wave(scene) and (scene.get('run') or {}).get('final_kill_settled') is True

    def observe(self, observation):
        scene = observation.get('scene') or {}
        if observation.get('complete') is not True or scene.get('available') is not True:
            self._reset_empty()
            return
        if scene.get('in_town') is True:
            self.__init__()
            return
        location = (scene.get('dungeon_id'), scene.get('difficulty'))
        if self.location is not None and location != self.location:
            self.__init__()
        run = scene.get('run') or {}
        if not isinstance(run, dict):
            run = {}
        won = run.get('status') == 'success' or (run.get('status') != 'failed'
            and run.get('completion_confirmed') is True)
        # A retained terminal record can briefly belong to the previous map.
        belongs = (str(run.get('dungeon_id', location[0])) == str(location[0])
                   and run.get('difficulty', location[1]) == location[1])
        if won and belongs and run.get('id') and run['id'] != self.run_id:
            self.__init__()
            self.run_id, self.location = run['id'], location
        if self.run_id is None:
            return
        # Online saving can cover the entire short win window. Preserve the
        # win above before waiting, just as the repeat counter does; a native
        # repeat may already have a different plan on our next readable tick.
        if any(scene.get(k) is True for k in ('scene_changing', 'teleporting', 'online_saving')):
            self._reset_empty()
            return
        ground = observation.get('ground') or {}
        if ground.get('complete') is not True or ground.get('items') != []:
            self._reset_empty()
        # The native completion flag/UI is later than the final kill: the game
        # waits for its loot spawner first. Built-in repeat may replace the plan
        # before we observe that flag, so a new plan is also completion evidence.
        popup = scene.get('completion_visible') or {}
        if (run.get('id') == self.run_id and (run.get('finished') is True
                or run.get('status') == 'success')
                or any(value is True for value in popup.values())
                or run.get('id') and run['id'] != self.run_id
                    and not run.get('retained_terminal')):
            self.settled = True

    def ready(self, observation, *, native_replay=False):
        self.observe(observation)
        if self.run_id is None:
            if native_replay:
                self.reason = '等待完整读取本局结算和地面奖励，暂不复刷。'
                return False
            return True
        scene = observation.get('scene') or {}
        ground = observation.get('ground') or {}
        if (observation.get('complete') is not True or scene.get('available') is not True or any(scene.get(k) is True
                for k in ('scene_changing', 'teleporting', 'online_saving'))
                or ground.get('complete') is not True
                or not isinstance(ground.get('items'), list)):
            self._reset_empty()
            self.reason = '等待完整读取本局地面奖励，暂不切图。'
            return False
        if ground['items']:
            self._reset_empty()
            self.reason = '先拾取本局地面奖励，再回城或切换副本。'
            return False
        if not self.settled:
            # Overlap the quiet period with the game's post-kill settlement,
            # never with ordinary combat or a retained win from a previous run.
            # Empty ground alone still cannot authorize departure: native
            # completion (after the loot spawner) must be observed above first.
            if self._final_kill_confirmed(scene):
                self._sample_empty()
            else:
                self._reset_empty()
            self.reason = '等待 Boss 掉落生成和本局结算完成，暂不切图。'
            return False
        run = scene.get('run') or {}
        popup = 'MythicCompletionUI' if scene.get('difficulty') == 'Mythic' else 'CombatMapCompletionUI'
        if (native_replay and self._current_final_wave(scene) and run.get('finished') is True
                and run.get('rewards_settled') is True
                and run.get('reward_evidence') == 'verified_WaveManager_finished_and_LootManager_idle'
                and (scene.get('completion_visible') or {}).get(popup) is True):
            # A current native completion proof replaces a heuristic delay.
            # Execution still independently re-reads ground and guards the
            # producer counter on the game thread before calling native replay.
            self.reason = ''
            return True
        self.reason = '正在确认本局地面奖励已收完，暂不切图。'
        return self._sample_empty()
