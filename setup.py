################################################################################
# Please note this setup.py is deprecated and only kept around to support      #
# existing installations configured with this method.                          #
################################################################################

from setuptools import setup


def main():
    setup(name='asl_zello_bridge',
          packages=['asl_zello_bridge'],
          entry_points={
              'console_scripts': [
                  'asl_zello_bridge = asl_zello_bridge.__main__:main'
              ]},
          install_requires=[
              'aiohttp',
              'cryptography',
              'pyjwt',
              'pyogg'
          ])


if __name__ == '__main__':
    main()
