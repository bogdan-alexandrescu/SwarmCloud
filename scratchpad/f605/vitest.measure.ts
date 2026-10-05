import { defineConfig, mergeConfig } from 'vitest/config'
import base from '../../apps/swarm-ui/vitest.config'
export default mergeConfig(base, defineConfig({ test: { setupFiles: [__dirname + '/measure-setup.ts'] } }))
