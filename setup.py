from setuptools import find_packages, setup

setup(
    name='TrafficToll',
    version='0.1.0',
    packages=find_packages(),
    url='',
    license='GPLv3',
    author='cryzed',
    author_email='cryzed@googlemail.com',
    description='NetLimiter-like traffic shaping for Linux',
    install_requires=['psutil', 'loguru', 'ruamel.yaml'],
    entry_points={
        'console_scripts': [
            'tt = traffictoll.cli:cli_main'
        ]
    }
)
