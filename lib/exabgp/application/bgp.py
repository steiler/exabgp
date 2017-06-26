# encoding: utf-8
"""
bgp.py

Created by Thomas Mangin on 2009-08-30.
Copyright (c) 2009-2017 Exa Networks. All rights reserved.
License: 3-clause BSD. (See the COPYRIGHT file)
"""

import os
import sys
import stat
import platform
import syslog
import string

from exabgp.util.dns import warn
from exabgp.logger import Logger

from exabgp.version import version
# import before the fork to improve copy on write memory savings
from exabgp.reactor.loop import Reactor

from exabgp.vendoring import docopt
from exabgp.vendoring import lsprofcalltree

from exabgp.configuration.usage import usage

from exabgp.debug import setup_report
setup_report()


def is_bgp (s):
	return all(c in string.hexdigits or c == ':' for c in s)


def __exit (memory, code):
	if memory:
		from exabgp.vendoring import objgraph
		sys.stdout.write('memory utilisation\n\n')
		sys.stdout.write(objgraph.show_most_common_types(limit=20))
		sys.stdout.write('\n\n\n')
		sys.stdout.write('generating memory utilisation graph\n\n')
		sys.stdout.write()
		obj = objgraph.by_type('Reactor')
		objgraph.show_backrefs([obj], max_depth=10)
	sys.exit(code)


def named_pipe (env, root):
	if not env.api.cli:
		return True
	locations = [
		'/run/%d/exabgp' % os.getuid(),
		'/run/exabgp',
		'/var/run/%d/exabgp' % os.getuid(),
		'/var/run/exabgp',
		root + '/run/%d/exabgp' % os.getuid(),
		root + '/run/exabgp',
		root + '/var/run/%d/exabgp' % os.getuid(),
		root + '/var/run/exabgp',
	]
	for location in locations:
		cli_in = location + '.in'
		cli_out = location + '.out'

		try:
			if not stat.S_ISFIFO(os.stat(cli_in).st_mode):
				continue
			if not stat.S_ISFIFO(os.stat(cli_out).st_mode):
				continue
		except KeyboardInterrupt:
			raise
		except Exception:
			continue
		os.environ['EXABGP_CLI_NAMED_PIPE'] = location
		return [location]
	return locations


def main ():
	major = int(sys.version[0])
	minor = int(sys.version[2])

	if major <= 2 and minor < 5:
		sys.stdout.write('This program can not work (is not tested) with your python version (< 2.5)\n')
		sys.stdout.flush()
		sys.exit(1)

	cli_named_pipe = os.environ.get('EXABGP_CLI_NAMED_PIPE','')
	if cli_named_pipe:
		from exabgp.application.control import main as control
		control(cli_named_pipe)
		sys.exit(0)

	options = docopt.docopt(usage, help=False)

	if options["--run"]:
		sys.argv = sys.argv[sys.argv.index('--run')+1:]
		if sys.argv[0] == 'healthcheck':
			from exabgp.application import run_healthcheck
			run_healthcheck()
		elif sys.argv[0] == 'cli':
			from exabgp.application import run_cli
			run_cli()
		else:
			sys.stdout.write(usage)
			sys.stdout.flush()
			sys.exit(0)
		return

	if options['--root']:
		root = os.path.realpath(os.path.normpath(options['--root'])).rstrip('/')
	elif sys.argv[0].endswith('/bin/exabgp'):
		root = sys.argv[0][:-len('/bin/exabgp')]
	elif sys.argv[0].endswith('/sbin/exabgp'):
		root = sys.argv[0][:-len('/sbin/exabgp')]
	else:
		root = ''

	etc = root + '/etc/exabgp'
	os.environ['EXABGP_ETC'] = etc  # This is not most pretty

	if options["--version"]:
		sys.stdout.write('ExaBGP : %s\n' % version)
		sys.stdout.write('Python : %s\n' % sys.version.replace('\n',' '))
		sys.stdout.write('Uname  : %s\n' % ' '.join(platform.uname()[:5]))
		sys.stdout.write('Root   : %s\n' % root)
		sys.stdout.flush()
		sys.exit(0)

	envfile = 'exabgp.env' if not options["--env"] else options["--env"]
	if not envfile.startswith('/'):
		envfile = '%s/%s' % (etc, envfile)

	from exabgp.configuration.setup import environment

	try:
		env = environment.setup(envfile)
	except environment.Error as exc:
		sys.stdout.write(usage)
		sys.stdout.flush()
		print('\nconfiguration issue,', str(exc))
		sys.exit(1)

	pipes = named_pipe(env,root)
	if len(pipes) != 1:
		sys.stdout.write('Could not find the named pipes for the cli in any of ' + ', '.join(pipes))
		sys.stdout.flush()
		return
	os.environ['EXABGP_CLI_NAMED_PIPE'] = pipes[0]
	sys.stdout.write('named pipes for the cli are %s.in & .out' % pipes[0])
	sys.stdout.flush()

	# Must be done before setting the logger as it modify its behaviour

	if options["--debug"]:
		env.log.all = True
		env.log.level = syslog.LOG_DEBUG

	logger = Logger()

	if options["--decode"]:
		decode = ''.join(options["--decode"]).replace(':','').replace(' ','')
		if not is_bgp(decode):
			sys.stdout.write(usage)
			sys.stdout.write('Environment values are:\n%s\n\n' % '\n'.join(' - %s' % _ for _ in environment.default()))
			sys.stdout.write('The BGP message must be an hexadecimal string.\n\n')
			sys.stdout.write('All colons or spaces are ignored, for example:\n\n')
			sys.stdout.write('  --decode 001E0200000007900F0003000101\n')
			sys.stdout.write('  --decode 001E:02:0000:0007:900F:0003:0001:01\n')
			sys.stdout.write('  --decode FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF001E0200000007900F0003000101\n')
			sys.stdout.write('  --decode FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF:001E:02:0000:0007:900F:0003:0001:01\n')
			sys.stdout.write('  --decode \'FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF 001E02 00000007900F0003000101\n\'')
			sys.stdout.flush()
			sys.exit(1)
	else:
		decode = ''

	duration = options["--signal"]
	if duration and duration.isdigit():
		pid = os.fork()
		if pid:
			import time
			import signal
			try:
				time.sleep(int(duration))
				os.kill(pid,signal.SIGUSR1)
			except KeyboardInterrupt:
				pass
			try:
				pid,code = os.wait()
				sys.exit(code)
			except KeyboardInterrupt:
				try:
					pid,code = os.wait()
					sys.exit(code)
				except Exception:
					sys.exit(0)

	if options["--help"]:
		sys.stdout.write(usage)
		sys.stdout.write('Environment values are:\n' + '\n'.join(' - %s' % _ for _ in environment.default()))
		sys.stdout.flush()
		sys.exit(0)

	if options["--decode"]:
		env.log.parser = True
		env.debug.route = decode
		env.tcp.bind = ''

	if options["--profile"]:
		env.profile.enable = True
		if options["--profile"].lower() in ['1','true']:
			env.profile.file = True
		elif options["--profile"].lower() in ['0','false']:
			env.profile.file = False
		else:
			env.profile.file = options["--profile"]

	if envfile and not os.path.isfile(envfile):
		comment = 'environment file missing\ngenerate it using "exabgp --fi > %s"' % envfile
	else:
		comment = ''

	if options["--full-ini"] or options["--fi"]:
		for line in environment.iter_ini():
			sys.stdout.write('%s\n' % line)
			sys.stdout.flush()
		sys.exit(0)

	if options["--full-env"] or options["--fe"]:
		print()
		for line in environment.iter_env():
			sys.stdout.write('%s\n' % line)
			sys.stdout.flush()
		sys.exit(0)

	if options["--diff-ini"] or options["--di"]:
		for line in environment.iter_ini(True):
			sys.stdout.write('%s\n' % line)
			sys.stdout.flush()
		sys.exit(0)

	if options["--diff-env"] or options["--de"]:
		for line in environment.iter_env(True):
			sys.stdout.write('%s\n' % line)
			sys.stdout.flush()
		sys.exit(0)

	if options["--once"]:
		env.tcp.once = True

	if options["--pdb"]:
		# The following may fail on old version of python (but is required for debug.py)
		os.environ['PDB'] = 'true'
		env.debug.pdb = True

	if options["--test"]:
		env.debug.selfcheck = True
		env.log.parser = True

	if options["--memory"]:
		env.debug.memory = True

	configurations = []
	# check the file only once that we have parsed all the command line options and allowed them to run
	if options["<configuration>"]:
		for f in options["<configuration>"]:
			normalised = os.path.realpath(os.path.normpath(f))
			if os.path.isfile(normalised):
				configurations.append(normalised)
				continue
			if f.startswith('etc/exabgp'):
				normalised = os.path.join(etc,f[11:])
				if os.path.isfile(normalised):
					configurations.append(normalised)
					continue

			logger.configuration('one of the arguments passed as configuration is not a file (%s)' % f,'error')
			sys.exit(1)

	else:
		sys.stdout.write(usage)
		sys.stdout.write('Environment values are:\n%s\n\n' % '\n'.join(' - %s' % _ for _ in environment.default()))
		sys.stdout.write('no configuration file provided')
		sys.stdout.flush()
		sys.exit(1)

	from exabgp.bgp.message.update.attribute import Attribute
	Attribute.caching = env.cache.attributes

	if env.debug.rotate or len(configurations) == 1:
		run(env,comment,configurations,root,options["--validate"])

	if not (env.log.destination in ('syslog','stdout','stderr') or env.log.destination.startswith('host:')):
		logger.configuration('can not log to files when running multiple configuration (as we fork)','error')
		sys.exit(1)

	try:
		# run each configuration in its own process
		pids = []
		for configuration in configurations:
			pid = os.fork()
			if pid == 0:
				run(env,comment,[configuration],root,options["--validate"],os.getpid())
			else:
				pids.append(pid)

		# If we get a ^C / SIGTERM, ignore just continue waiting for our child process
		import signal
		signal.signal(signal.SIGINT, signal.SIG_IGN)

		# wait for the forked processes
		for pid in pids:
			os.waitpid(pid,0)
	except OSError as exc:
		logger.reactor('Can not fork, errno %d : %s' % (exc.errno,exc.strerror),'critical')
		sys.exit(1)


def run (env, comment, configurations, root, validate, pid=0):
	logger = Logger()

	logger.error('',source='ExaBGP')
	logger.error('%s' % version,source='version')
	logger.error('%s' % sys.version.replace('\n',' '),source='interpreter')
	logger.error('%s' % ' '.join(platform.uname()[:5]),source='os')
	logger.error('',source='ExaBGP')

	if comment:
		logger.configuration(comment)

	warning = warn()
	if warning:
		logger.configuration(warning)

	if not env.profile.enable:
		was_ok = Reactor(configurations).run(validate,root)
		__exit(env.debug.memory,0 if was_ok else 1)

	try:
		import cProfile as profile
	except ImportError:
		import profile

	if env.profile.file == 'stdout':
		profiled = 'Reactor(%s).run(%s,"%s")' % (str(configurations),str(validate),str(root))
		was_ok = profile.run(profiled)
		__exit(env.debug.memory,0 if was_ok else 1)

	if pid:
		profile_name = "%s-pid-%d" % (env.profile.file,pid)
	else:
		profile_name = env.profile.file

	notice = ''
	if os.path.isdir(profile_name):
		notice = 'profile can not use this filename as output, it is not a directory (%s)' % profile_name
	if os.path.exists(profile_name):
		notice = 'profile can not use this filename as output, it already exists (%s)' % profile_name

	if not notice:
		cwd = os.getcwd()
		logger.reactor('profiling ....')
		profiler = profile.Profile()
		profiler.enable()
		try:
			was_ok = Reactor(configurations).run(validate,root)
		except Exception:
			was_ok = False
			raise
		finally:
			profiler.disable()
			kprofile = lsprofcalltree.KCacheGrind(profiler)
			try:
				destination = profile_name if profile_name.startswith('/') else os.path.join(cwd,profile_name)
				with open(destination, 'w+') as write:
					kprofile.output(write)
			except IOError:
				notice = 'could not save profiling in formation at: ' + destination
				logger.reactor("-"*len(notice))
				logger.reactor(notice)
				logger.reactor("-"*len(notice))
			__exit(env.debug.memory,0 if was_ok else 1)
	else:
		logger.reactor("-"*len(notice))
		logger.reactor(notice)
		logger.reactor("-"*len(notice))
		Reactor(configurations).run(validate,root)
		__exit(env.debug.memory,1)


if __name__ == '__main__':
	main()
