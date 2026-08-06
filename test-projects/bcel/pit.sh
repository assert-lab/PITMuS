mvn clean test-compile 2>&1 | tee mvn.log
mvn pitest:mutationCoverage -Dfeatures=+EXPORT -DexcludedTestClasses='<failing.test.Class>' 2>&1 | tee pit.log