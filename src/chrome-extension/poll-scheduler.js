/**
 * 单例轮询器（CARD-09b 第 6 条）。
 *
 * 侧栏会被反复关闭、重开、连点「开始会审」，一不小心就会叠出好几个
 * setInterval —— 请求翻倍、状态互相覆盖。这个类只保证三件事：
 *
 *   1. start() 幂等：已经有定时器就直接返回 false，绝不再排一个；
 *   2. 同一时刻最多一个 tick 在跑：上一轮没结束就跳过本轮；
 *   3. stop() 之后不再自己复活（要重新 start() 才会继续）。
 *
 * 计时器可注入，方便在没有浏览器的地方用假定时器验证（tests\test_card09b.py）。
 */
export class PollScheduler {
  constructor(tick, { interval = 1500, setTimer, clearTimer } = {}) {
    if (typeof tick !== "function") throw new TypeError("tick must be a function");
    this.tick = tick;
    this.interval = interval;
    this.setTimer = setTimer || ((fn, ms) => setTimeout(fn, ms));
    this.clearTimer = clearTimer || ((id) => clearTimeout(id));
    this.timer = null;
    this.busy = false;
    this.stopped = true;
    this.ticks = 0;
  }

  get running() {
    return this.timer !== null;
  }

  /** 开跑；已经在跑（或正在 tick 中）就不叠加。返回是否真的排了新定时器。 */
  start() {
    this.stopped = false;
    if (this.timer !== null || this.busy) return false;
    this._schedule();
    return true;
  }

  /** 停跑并清掉待触发的定时器；可重复调用。 */
  stop() {
    this.stopped = true;
    if (this.timer !== null) {
      this.clearTimer(this.timer);
      this.timer = null;
    }
  }

  /** 立刻来一轮：清掉待触发的定时器再排一个 0 等待的，之后仍按 interval 继续。 */
  kick() {
    if (this.timer !== null) {
      this.clearTimer(this.timer);
      this.timer = null;
    }
    if (this.busy) return false;
    this.stopped = false;
    this.timer = this.setTimer(() => {
      this.timer = null;
      void this._run();
    }, 0);
    return true;
  }

  _schedule() {
    if (this.stopped || this.timer !== null) return;
    this.timer = this.setTimer(() => {
      this.timer = null;
      void this._run();
    }, this.interval);
  }

  async _run() {
    if (this.stopped) return;
    if (this.busy) {
      this._schedule();
      return;
    }
    this.busy = true;
    this.ticks += 1;
    try {
      await this.tick();
    } catch {
      // tick 自己负责把错误展示在界面上，这里只保证轮询不中断。
    } finally {
      this.busy = false;
    }
    this._schedule();
  }
}
