// One policy controller for configuration, imports, history, and rendered results.
(function (root) {
  const MOCK_POLICY = Object.freeze({demo_min_group_size: null, row_limit: 200, policy_version: 1});

  function validate(config) {
    const keys = ['demo_min_group_size', 'row_limit', 'policy_version'];
    if (!config || Array.isArray(config) || Object.keys(config).length !== keys.length ||
        !keys.every((key) => Object.hasOwn(config, key)) ||
        !(config.demo_min_group_size === null || Number.isInteger(config.demo_min_group_size) && config.demo_min_group_size > 0) ||
        config.row_limit !== 200 || config.policy_version !== 1) throw Error('Invalid privacy policy.');
    return Object.freeze({...config});
  }

  function createPolicy({fetcher = (...args) => root.fetch(...args), timeoutMs = 10000,
                         url = () => `${root.SQ?.config?.API_BASE || ''}/demo/config`} = {}) {
    let state = 'loading', config = null, generation = 0;
    const subscribers = new Set();
    const notify = () => subscribers.forEach((callback) => callback(controller));
    const controller = {
      get state() { return state; },
      get config() { return config; },
      subscribe(callback) {
        subscribers.add(callback);
        callback(controller);
        return () => subscribers.delete(callback);
      },
      async load({mock = false} = {}) {
        const current = ++generation;
        config = null; state = 'loading'; notify();
        const abort = new AbortController();
        let timer;
        try {
          const payload = mock ? MOCK_POLICY : await Promise.race([
            (async () => {
              const response = await fetcher(url(), {signal: abort.signal, cache: 'no-store'});
              if (!response.ok) throw Error('Privacy policy unavailable');
              return response.json();
            })(),
            new Promise((_, reject) => { timer = setTimeout(() => {
              abort.abort(); reject(Error('Privacy policy timeout'));
            }, timeoutMs); }),
          ]);
          const validated = validate(payload);
          if (current !== generation) return controller.state;
          config = validated; state = 'available';
        } catch (_) {
          if (current !== generation) return controller.state;
          config = null; state = 'failed';
        } finally { clearTimeout(timer); }
        notify();
        return state;
      },
    };
    return controller;
  }

  if (typeof module !== 'undefined') module.exports = {MOCK_POLICY, validate, createPolicy};
  else { root.SQ = root.SQ || {}; root.SQ.policy = createPolicy(); }
})(typeof window === 'undefined' ? globalThis : window);
