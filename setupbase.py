from __future__ import print_function, absolute_import
import os
import string
import shutil
import subprocess
import tempfile
from types import ListType, TupleType
from distutils.dep_util import newer_group
from distutils.core import Extension
from distutils.errors import DistutilsExecError
from distutils.ccompiler import new_compiler
from distutils.sysconfig import customize_compiler, get_config_vars
from distutils.command.build_ext import build_ext as _build_ext


def find_packages():
    """Find all of mdtraj's python packages.
    Adapted from IPython's setupbase.py. Copyright IPython
    contributors, licensed under the BSD license.
    """
    packages = ['mdtraj.scripts']
    for dir,subdirs,files in os.walk('MDTraj'):
        package = dir.replace(os.path.sep, '.')
        if '__init__.py' not in files:
            # not a package
            continue
        packages.append(package.replace('MDTraj', 'mdtraj'))
    return packages



################################################################################
# Detection of compiler capabilities
################################################################################

class CompilerDetection(object):
    # Necessary for OSX. See https://github.com/mdtraj/mdtraj/issues/576
    # The problem is that distutils.sysconfig.customize_compiler()
    # is necessary to properly invoke the correct compiler for this class
    # (otherwise the CC env variable isn't respected). Unfortunately,
    # distutils.sysconfig.customize_compiler() DIES on OSX unless some
    # appropriate initialization routines have been called. This line
    # has a side effect of calling those initialzation routes, and is therefor
    # necessary for OSX, even though we don't use the result.
    _DONT_REMOVE_ME = get_config_vars()

    def __init__(self, disable_openmp):
        cc = new_compiler()
        customize_compiler(cc)

        self.msvc = cc.compiler_type == 'msvc'
        self._print_compiler_version(cc)

        if disable_openmp:
            self.openmp_enabled = False
        else:
            self.openmp_enabled, openmp_needs_gomp = self._detect_openmp()
        self.sse3_enabled = self._detect_sse3() if not self.msvc else True
        self.sse41_enabled = self._detect_sse41() if not self.msvc else True

        self.compiler_args_sse2  = ['-msse2'] if not self.msvc else ['/arch:SSE2']
        self.compiler_args_sse3  = ['-mssse3'] if (self.sse3_enabled and not self.msvc) else []

        self.compiler_args_sse41, self.define_macros_sse41 = [], []
        if self.sse41_enabled:
            self.define_macros_sse41 = [('__SSE4__', 1), ('__SSE4_1__', 1)]
            if not self.msvc:
                self.compiler_args_sse41 = ['-msse4']

        if self.openmp_enabled:
            self.compiler_libraries_openmp = []

            if self.msvc:
                self.compiler_args_openmp = ['/openmp']
            else:
                self.compiler_args_openmp = ['-fopenmp']
                if openmp_needs_gomp:
                    self.compiler_libraries_openmp = ['gomp']
        else:
            self.compiler_libraries_openmp = []
            self.compiler_args_openmp = []

        if self.msvc:
            self.compiler_args_opt = ['/O2']
        else:
            self.compiler_args_opt = ['-O3', '-funroll-loops']
        print()

    def _print_compiler_version(self, cc):
        print("C compiler:")
        try:
            if self.msvc:
                if not cc.initialized:
                    cc.initialize()
                cc.spawn([cc.cc])
            else:
                cc.spawn([cc.compiler[0]] + ['-v'])
        except DistutilsExecError:
            pass

    def hasfunction(self, cc, funcname, include=None, extra_postargs=None):
        # From http://stackoverflow.com/questions/
        #            7018879/disabling-output-when-compiling-with-distutils
        tmpdir = tempfile.mkdtemp(prefix='hasfunction-')
        devnull = oldstderr = None
        try:
            try:
                fname = os.path.join(tmpdir, 'funcname.c')
                f = open(fname, 'w')
                if include is not None:
                    f.write('#include %s\n' % include)
                f.write('int main(void) {\n')
                f.write('    %s;\n' % funcname)
                f.write('}\n')
                f.close()
                devnull = open(os.devnull, 'w')
                oldstderr = os.dup(sys.stderr.fileno())
                os.dup2(devnull.fileno(), sys.stderr.fileno())
                objects = cc.compile([fname], output_dir=tmpdir,
                                     extra_postargs=extra_postargs)
                cc.link_executable(objects, os.path.join(tmpdir, 'a.out'))
            except Exception as e:
                return False
            return True
        finally:
            if oldstderr is not None:
                os.dup2(oldstderr, sys.stderr.fileno())
            if devnull is not None:
                devnull.close()
            shutil.rmtree(tmpdir)

    def _print_support_start(self, feature):
        print('Attempting to autodetect {0:6} support...'.format(feature), end=' ')

    def _print_support_end(self, feature, status):
        if status is True:
            print('Compiler supports {0}'.format(feature))
        else:
            print('Did not detect {0} support'.format(feature))

    def _detect_openmp(self):
        self._print_support_start('OpenMP')
        compiler = new_compiler()
        customize_compiler(compiler)
        hasopenmp = self.hasfunction(compiler, 'omp_get_num_threads()', extra_postargs=['-fopenmp', '/openmp'])
        needs_gomp = hasopenmp
        if not hasopenmp:
            compiler.add_library('gomp')
            hasopenmp = self.hasfunction(compiler, 'omp_get_num_threads()')
            needs_gomp = hasopenmp
        self._print_support_end('OpenMP', hasopenmp)
        return hasopenmp, needs_gomp

    def _detect_sse3(self):
        "Does this compiler support SSE3 intrinsics?"
        compiler = new_compiler()
        customize_compiler(compiler)
        self._print_support_start('SSE3')
        result = self.hasfunction(compiler, '__m128 v; _mm_hadd_ps(v,v)',
                           include='<pmmintrin.h>',
                           extra_postargs=['-msse3'])
        self._print_support_end('SSE3', result)
        return result

    def _detect_sse41(self):
        "Does this compiler support SSE4.1 intrinsics?"
        compiler = new_compiler()
        customize_compiler(compiler)
        self._print_support_start('SSE4.1')
        result = self.hasfunction(compiler, '__m128 v; _mm_round_ps(v,0x00)',
                           include='<smmintrin.h>',
                           extra_postargs=['-msse4'])
        self._print_support_end('SSE4.1', result)
        return result

################################################################################
# Writing version control information to the module
################################################################################

def git_version():
    # Return the git revision as a string
    # copied from numpy setup.py
    def _minimal_ext_cmd(cmd):
        # construct minimal environment
        env = {}
        for k in ['SYSTEMROOT', 'PATH']:
            v = os.environ.get(k)
            if v is not None:
                env[k] = v
        # LANGUAGE is used on win32
        env['LANGUAGE'] = 'C'
        env['LANG'] = 'C'
        env['LC_ALL'] = 'C'
        out = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, env=env).communicate()[0]
        return out

    try:
        out = _minimal_ext_cmd(['git', 'rev-parse', 'HEAD'])
        GIT_REVISION = out.strip().decode('ascii')
    except OSError:
        GIT_REVISION = 'Unknown'

    return GIT_REVISION


def write_version_py(VERSION, ISRELEASED, filename='MDTraj/version.py'):
    cnt = """
# THIS FILE IS GENERATED FROM MDTRAJ SETUP.PY
short_version = '%(version)s'
version = '%(version)s'
full_version = '%(full_version)s'
git_revision = '%(git_revision)s'
release = %(isrelease)s

if not release:
    version = full_version
"""
    # Adding the git rev number needs to be done inside write_version_py(),
    # otherwise the import of numpy.version messes up the build under Python 3.
    FULLVERSION = VERSION
    if os.path.exists('.git'):
        GIT_REVISION = git_version()
    else:
        GIT_REVISION = 'Unknown'

    if not ISRELEASED:
        FULLVERSION += '.dev-' + GIT_REVISION[:7]

    a = open(filename, 'w')
    try:
        a.write(cnt % {'version': VERSION,
                       'full_version': FULLVERSION,
                       'git_revision': GIT_REVISION,
                       'isrelease': str(ISRELEASED)})
    finally:
        a.close()
    

class StaticLibrary(Extension):
    def __init__(self, *args, **kwargs):
        self.export_include = kwargs.pop('export_include', [])
        Extension.__init__(self, *args, **kwargs)


class build_ext(_build_ext):

    def build_extension(self, ext):
        if isinstance(ext, StaticLibrary):
            self.build_static_extension(ext)
        else:
            _build_ext.build_extension(self, ext)

    def build_static_extension(self, ext):
        from distutils import log
        
        sources = ext.sources
        if sources is None or type(sources) not in (ListType, TupleType):
            raise DistutilsSetupError, \
                  ("in 'ext_modules' option (extension '%s'), " +
                   "'sources' must be present and must be " +
                   "a list of source filenames") % ext.name
        sources = list(sources)

        ext_path = self.get_ext_fullpath(ext.name)
        depends = sources + ext.depends
        if not (self.force or newer_group(depends, ext_path, 'newer')):
            log.debug("skipping '%s' extension (up-to-date)", ext.name)
            # return (DEBUG)
        else:
            log.info("building '%s' extension", ext.name)

        extra_args = ext.extra_compile_args or []
        macros = ext.define_macros[:]
        for undef in ext.undef_macros:
            macros.append((undef,))
        objects = self.compiler.compile(sources,
                                         output_dir=self.build_temp,
                                         macros=macros,
                                         include_dirs=ext.include_dirs,
                                         debug=self.debug,
                                         extra_postargs=extra_args,
                                         depends=ext.depends)
        self._built_objects = objects[:]
        if ext.extra_objects:
            objects.extend(ext.extra_objects)
        extra_args = ext.extra_link_args or []
        
        language = ext.language or self.compiler.detect_language(sources)

        libname = os.path.splitext(os.path.basename(ext_path))[0]
        output_dir = os.path.dirname(ext_path)
        if (self.compiler.static_lib_format.startswith('lib') and
            libname.startswith('lib')):
            libname = libname[3:]

        self.compiler.create_static_lib(objects, 
            output_libname=libname,
            output_dir=output_dir,
            target_lang=language)

        for item in ext.export_include:
            shutil.copy(item, output_dir)