
yum install gcc-c++ git -y
yum install python-setuptools -y
yum install bz2 -y
easy_install pip numpy cython
python setup.py sdist bdist_wheel
twine upload dist/*

