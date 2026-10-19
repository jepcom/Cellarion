"""Validate the deployment contract without starting containers or using secrets."""
import json
import os
from pathlib import Path
import subprocess
import unittest

COMPOSE = Path(__file__).with_name('docker-compose.yml')
ENV = {key: value for key, value in os.environ.items()
       if key in ('PATH', 'HOME', 'DOCKER_CONFIG')}
ENV.update(JWT_SECRET='public-test-placeholder-not-a-secret',
           MEILI_MASTER_KEY='public-test-placeholder-not-a-secret')


def render(env):
    return subprocess.run(
        ['docker', 'compose', '--env-file', '/dev/null', '-f', str(COMPOSE),
         'config', '--format', 'json'],
        env=env, capture_output=True, text=True, check=False)


class ComposeContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = render(ENV)
        if result.returncode:
            raise RuntimeError(result.stderr)
        cls.config = json.loads(result.stdout)
        cls.services = cls.config['services']

    def test_network_and_storage(self):
        self.assertEqual(set(self.services),
                         {'mongo', 'meilisearch', 'backend', 'rembg', 'frontend'})
        for name, service in self.services.items():
            self.assertFalse(service.get('ports'), name)
            self.assertNotIn('container_name', service)
            self.assertNotIn('build', service)
            self.assertEqual(service['labels']['traefik.enable'],
                             'true' if name == 'frontend' else 'false')
        self.assertEqual(self.services['frontend']['expose'], ['8080'])
        mounts = self.services['backend']['volumes']
        self.assertEqual([(v['type'], v['source'], v['target']) for v in mounts],
                         [('volume', 'uploads-data', '/app/uploads')])
        self.assertEqual(set(self.config['volumes']),
                         {'mongo-data', 'meili-data', 'uploads-data'})

    def test_images_and_hardening(self):
        images = {
            'mongo': 'mongo:7@sha256:340c1c56fb10e95cf79ff547f8664b96bc6ead9909bc355238cbf865a9695a6f',
            'meilisearch': 'getmeili/meilisearch:v1.45.1',
            **{name: f'ghcr.io/jagduvi1/cellarion-{name}:1.274.0'
               for name in ('backend', 'frontend', 'rembg')},
        }
        limits = dict(mongo=512, meilisearch=256, backend=640, rembg=1536, frontend=64)
        for name, service in self.services.items():
            self.assertEqual(service['image'], images[name])
            self.assertEqual(service['restart'], 'unless-stopped')
            self.assertEqual(service['cap_drop'], ['ALL'])
            self.assertEqual(service['security_opt'], ['no-new-privileges:true'])
            self.assertEqual(service['deploy']['resources']['limits']['memory'],
                             str(limits[name] * 1024 * 1024))
            if name != 'frontend':
                self.assertIn('healthcheck', service)
            if name != 'mongo':
                self.assertFalse(service.get('cap_add'))
        self.assertEqual(set(self.services['mongo']['cap_add']),
                         {'CHOWN', 'DAC_OVERRIDE', 'SETGID', 'SETUID'})

    def test_environment_and_required_secrets(self):
        backend = self.services['backend']['environment']
        for key in ('FRONTEND_URL', 'BACKEND_URL'):
            self.assertEqual(backend[key], 'https://wines.exerun.com')
        self.assertEqual(backend['SUPER_ADMIN_EMAIL'], 'jb@jepcom.dk')
        self.assertEqual(backend['TRUST_PROXY_HOPS'], '2')
        self.assertEqual(backend['MEILI_MASTER_KEY'],
                         self.services['meilisearch']['environment']['MEILI_MASTER_KEY'])
        self.assertEqual(set(backend), {
            'JWT_SECRET', 'MEILI_MASTER_KEY', 'FRONTEND_URL', 'BACKEND_URL',
            'SUPER_ADMIN_EMAIL', 'TRUST_PROXY_HOPS', 'NODE_ENV', 'NODE_OPTIONS',
            'MONGO_URI', 'PORT', 'MEILI_URL', 'REMBG_URL'})
        for key in ('JWT_SECRET', 'MEILI_MASTER_KEY'):
            for value in (None, ''):
                env = ENV.copy()
                if value is None:
                    env.pop(key)
                else:
                    env[key] = value
                result = render(env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(key, result.stderr)


if __name__ == '__main__':
    unittest.main()
